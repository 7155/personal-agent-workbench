"""Trusted project delivery around isolated, agent-writable workspaces.

The team service never asks Git to inspect an agent-owned checkout.  A
workspace is handed to a Session as a plain directory (and may contain its
own ``.git`` metadata), while snapshots are read with descriptor-relative,
no-follow traversal and materialized into a server-owned repository.  Git
operations used for commits, merges, and ref updates therefore run only in
repositories created and controlled by this service.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
import hashlib
import inspect
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any, cast

import sqlite3

from ..db import sqlite_connection
from .errors import TeamError
from .grants import TeamGrantStore
from .identity import TeamIdentityStore


_MAX_DESCRIPTION = 2_000
_MAX_BRANCH = 128
_MAX_WORKSPACE_FILES = 4_096
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_EXCLUDED_PATHS = 64
_MAX_EXCLUDED_PATH_LENGTH = 512
_GIT_TIMEOUT_SECONDS = 60
_SQLITE_BUSY_TIMEOUT_MS = 5_000
_MAX_REQUIREMENTS_REVISION = 2**31 - 1
_IDENTIFIER_RE = re.compile(r"^[^/\\\x00]{1,256}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_HEX_OBJECT_RE = re.compile(r"^[0-9a-f]{40,64}$")

_WRITE_ROLES = frozenset({"owner", "maintainer", "contributor"})
_MANAGE_ROLES = frozenset({"owner", "maintainer"})

# These names are intentionally conservative.  A team source repository
# should keep credentials and runtime state outside an Agent workspace; if a
# caller does put explicit credential/config files there, delivery omits them
# by default.  Generic directory names such as ``runtime`` or ``sessions``
# are valid source layouts and are deliberately retained.  This bounded list
# is a delivery safeguard, not a complete secret detector.
_PRIVATE_NAMES = frozenset(
    {
        ".codex",
        ".pi",
        ".paw",
        ".netrc",
        "credentials",
        "credentials.json",
        "secrets",
        "secrets.json",
        "auth.json",
        "token.json",
        "id_rsa",
        "id_ed25519",
    }
)
_PRIVATE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".crt"})


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def _invalid(field: str, message: str = "is invalid") -> TeamError:
    return TeamError(400, "invalid_input", f"{field} {message}")


def _text(value: object, field: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _invalid(field, "must be text")
    if "\x00" in value:
        raise _invalid(field, "contains an invalid character")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise _invalid(field, "is required")
    if len(normalized) > maximum:
        raise _invalid(field, "is too long")
    return normalized


def _identifier(value: object, field: str) -> str:
    normalized = _text(value, field, 256)
    if not _IDENTIFIER_RE.fullmatch(normalized) or normalized in {".", ".."}:
        raise _invalid(field, "contains a path separator")
    return normalized


def _branch(value: object) -> str:
    normalized = _text(value, "target_branch", _MAX_BRANCH)
    if (
        not _BRANCH_RE.fullmatch(normalized)
        or ".." in normalized
        or normalized.endswith("/")
        or "//" in normalized
        or normalized.endswith(".")
        or normalized.endswith(".lock")
    ):
        raise _invalid("target_branch", "is not a safe Git branch")
    return normalized


def _object_id(value: object, field: str) -> str:
    normalized = _text(value, field, 128)
    if not _HEX_OBJECT_RE.fullmatch(normalized.casefold()):
        raise TeamError(500, "repository_corrupt", "repository object identity is invalid")
    return normalized.casefold()


def _requirements_revision(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(field, "must be an integer")
    if not 0 <= value <= _MAX_REQUIREMENTS_REVISION:
        raise _invalid(field, "is out of range")
    return value


def _space_not_found() -> TeamError:
    return TeamError(404, "space_not_found", "space not found")


def _not_found(message: str = "draft not found") -> TeamError:
    return TeamError(404, "resource_not_found", message)


def _denied() -> TeamError:
    return TeamError(403, "execution_not_authorized", "This execution is no longer authorized")


def _safe_json(value: object, *, maximum: int = 8_192) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = json.dumps({"ok": False, "error": "non_serializable_verifier_result"})
    if len(encoded.encode("utf-8")) > maximum:
        return json.dumps({"ok": False, "truncated": True}, separators=(",", ":"))
    return encoded


def _load_json_object(raw: object, *, fallback: dict[str, object] | None = None) -> dict[str, object]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {} if fallback is None else dict(fallback)
    return dict(value) if isinstance(value, Mapping) else ({} if fallback is None else dict(fallback))


def _json_list(raw: object) -> list[object]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(value) if isinstance(value, list) else []


def _safe_verifier_result(value: object) -> tuple[bool, dict[str, object]]:
    if isinstance(value, bool):
        return value, {"ok": value}
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if isinstance(key, str) and len(key) <= 128:
                result[key] = _bound_verifier_value(item)
        ok = result.get("ok", result.get("success", result.get("passed", False))) is True
        try:
            json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            result = {"ok": bool(ok), "error": "non_serializable_verifier_result"}
        return bool(ok), result
    return bool(value), {"ok": bool(value)}


def _bound_verifier_value(value: object, *, depth: int = 0) -> object:
    """Keep verifier receipts useful without allowing unbounded output."""

    if isinstance(value, str):
        return value[:2_048]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 2:
        return str(value)[:512]
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: _bound_verifier_value(item, depth=depth + 1)
            for key, item in list(value.items())[:32]
        }
    if isinstance(value, (list, tuple)):
        return [_bound_verifier_value(item, depth=depth + 1) for item in value[:32]]
    return str(value)[:512]


def _call_compatible(callback: Callable[..., object], options: Sequence[tuple[object, ...]]) -> object:
    """Call a callback using the first signature-compatible option.

    ``inspect.signature(...).bind`` distinguishes callback signature mismatch
    from a callback that itself raises ``TypeError``; the latter is allowed to
    propagate to the caller and is not retried with a different contract.
    """

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(*options[0])
    for arguments in options:
        try:
            signature.bind(*arguments)
        except TypeError:
            continue
        return callback(*arguments)
    raise TypeError("callback does not implement a supported signature")


def _safe_component(value: str) -> str:
    # IDs have already been validated; this extra check keeps path construction
    # robust if a future caller loads an old row containing malformed metadata.
    if not _IDENTIFIER_RE.fullmatch(value) or value in {".", ".."}:
        raise TeamError(500, "workspace_metadata_invalid", "workspace metadata is invalid")
    return value


def _safe_relative_path(value: object, field: str = "path") -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise TeamError(400, "unsafe_workspace", "workspace contains an unsafe path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise TeamError(400, "unsafe_workspace", "workspace contains an unsafe path")
    if any(ord(char) < 32 for char in value):
        raise TeamError(400, "unsafe_workspace", "workspace contains an unsafe path")
    normalized = "/".join(path.parts)
    if len(normalized) > 1_024:
        raise TeamError(400, "unsafe_workspace", "workspace path is too long")
    return normalized


def _is_private_path(relative: str) -> bool:
    for part in relative.split("/"):
        lowered = part.casefold()
        if lowered == ".gitattributes":
            # Attribute files can select external filters and merge drivers.
            return True
        if lowered in _PRIVATE_NAMES or lowered.startswith(".env"):
            return True
        if any(lowered.endswith(suffix) for suffix in _PRIVATE_SUFFIXES):
            return True
    return False


def _git_executable() -> str:
    for candidate in ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git"):
        if os.access(candidate, os.X_OK):
            return candidate
    return "git"


_GIT = _git_executable()


class TeamWorkspaceService:
    """Server-owned project repositories and immutable delivery snapshots."""

    def __init__(
        self,
        data_root: str | Path,
        identity: TeamIdentityStore,
        grants: TeamGrantStore,
        quiesce: Callable[..., object] | None = None,
        verifier: Callable[..., object] | None = None,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        raw_root = Path(data_root).expanduser()
        if raw_root.is_symlink():
            raise ValueError("team workspace data root cannot be a symlink")
        self.data_root = raw_root.resolve(strict=False)
        if self.data_root == Path("/"):
            raise ValueError("team workspace data root cannot be the filesystem root")
        self.identity = identity
        self.grants = grants
        if getattr(grants, "identity", identity) is not identity:
            raise ValueError("team workspace grants and identity must be the same store")
        self.quiesce = quiesce
        self.verifier = verifier
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.projects_root = self.data_root / "projects"
        self.workspaces_root = self.data_root / "workspaces"
        self.staging_root = self.data_root / "staging"
        for directory in (self.data_root, self.projects_root, self.workspaces_root, self.staging_root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        self._lock = threading.RLock()

    def _now_ms(self) -> int:
        try:
            return int(self.clock())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("team workspace clock must return milliseconds") from exc

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self.identity.initialize()
        conn_context = sqlite_connection(
            self.identity.db_path,
            row_factory=sqlite3.Row,
            foreign_keys=True,
        )
        conn = conn_context.__enter__()
        try:
            conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
        except BaseException:
            conn_context.__exit__(*sys.exc_info())
            raise
        else:
            conn_context.__exit__(None, None, None)

    def _git_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        for key in tuple(env):
            if key.startswith("GIT_"):
                env.pop(key, None)
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_PAGER": "cat",
                "GIT_EDITOR": ":",
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "LC_ALL": "C",
                "LANG": "C",
            }
        )
        if extra:
            env.update({str(key): str(value) for key, value in extra.items()})
        return env

    def _git_run(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        input_data: bytes | str | None = None,
        extra_env: Mapping[str, str] | None = None,
        timeout: int = _GIT_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        command = [
            _GIT,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "core.autocrlf=false",
            *[str(argument) for argument in args],
        ]
        byte_output = isinstance(input_data, bytes)
        try:
            return subprocess.run(
                command,
                cwd=str(cwd),
                env=self._git_env(extra_env),
                input=input_data,
                capture_output=True,
                check=False,
                timeout=timeout,
                text=not byte_output,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TeamError(500, "git_operation_failed", "trusted repository operation failed") from exc

    def _git_checked(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        input_data: bytes | str | None = None,
        extra_env: Mapping[str, str] | None = None,
        timeout: int = _GIT_TIMEOUT_SECONDS,
    ) -> str:
        completed = self._git_run(args, cwd, input_data=input_data, extra_env=extra_env, timeout=timeout)
        if completed.returncode != 0:
            raise TeamError(500, "git_operation_failed", "trusted repository operation failed")
        output = completed.stdout
        if isinstance(output, bytes):
            return output.decode("utf-8", errors="strict")
        return str(output)

    def _git_bytes(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        timeout: int = _GIT_TIMEOUT_SECONDS,
    ) -> bytes:
        # Supplying bytes forces ``subprocess`` to preserve Git's binary tar
        # stream; decoding an archive as text corrupts even an empty tree.
        completed = self._git_run(args, cwd, input_data=b"", timeout=timeout)
        if completed.returncode != 0:
            raise TeamError(500, "git_operation_failed", "trusted repository operation failed")
        output = completed.stdout
        if isinstance(output, str):
            return output.encode("utf-8")
        if len(output) > _MAX_ARCHIVE_BYTES:
            raise TeamError(400, "workspace_too_large", "repository snapshot is too large")
        return bytes(output)

    def _repo_revision(self, repository: Path, target_branch: str) -> str:
        ref = f"refs/heads/{target_branch}"
        result = self._git_checked(["rev-parse", "--verify", ref], repository).strip()
        return _object_id(result, "commit")

    def _repository_path(self, space_id: str, target_branch: str | None = None) -> Path:
        # One trusted object database serves all target branches for a
        # project; SQLite rows remain keyed by (space_id, target_branch) so
        # branch-specific revisions and integration CAS state stay separate.
        del target_branch
        return self.projects_root / _safe_component(space_id) / "repository"

    def _workspace_path(self, space_id: str, workspace_id: str) -> Path:
        return self.workspaces_root / _safe_component(space_id) / _safe_component(workspace_id)

    def _trusted_source(self, source_repo: str | Path) -> Path:
        raw = Path(source_repo).expanduser()
        if raw.exists() and raw.is_symlink():
            raise _invalid("source_repo", "cannot be a symlink")
        try:
            source = raw.resolve(strict=True)
        except OSError as exc:
            raise TeamError(404, "source_not_found", "trusted source repository was not found") from exc
        if not source.is_dir() or not (source / ".git").exists():
            raise TeamError(400, "invalid_source_repo", "trusted source repository is invalid")
        if source == self.data_root or self.data_root in source.parents:
            raise TeamError(400, "invalid_source_repo", "source repository must be outside team workspaces")
        return source

    def _space_row(self, conn: sqlite3.Connection, space_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM team_spaces WHERE id = ?", (space_id,)).fetchone()
        if row is None:
            raise _space_not_found()
        return cast(sqlite3.Row, row)

    def _project_row(
        self,
        conn: sqlite3.Connection,
        space_id: str,
        target_branch: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM team_projects WHERE space_id = ? AND target_branch = ?",
            (space_id, target_branch),
        ).fetchone()
        if row is None:
            # Session creation needs a private repository even when the
            # personal space has never been used before.  Project repositories
            # remain an explicit server provisioning step so a missing source
            # cannot silently discard a project baseline.
            space = self._space_row(conn, space_id)
            if str(space["kind"]) == "personal":
                repository = self._repository_path(space_id, target_branch)
                created_repository = False
                try:
                    if repository.exists() and not repository.is_symlink():
                        repository = self._assert_repo_path(repository)
                        sibling = conn.execute(
                            "SELECT * FROM team_projects WHERE space_id = ? ORDER BY updated_at_ms DESC, target_branch LIMIT 1",
                            (space_id,),
                        ).fetchone()
                        if sibling is None:
                            raise TeamError(409, "project_repository_conflict", "project repository already exists")
                        ref = f"refs/heads/{target_branch}"
                        branch_result = self._git_run(["rev-parse", "--verify", ref], repository)
                        if branch_result.returncode == 0:
                            output = branch_result.stdout
                            head = str(output).strip() if not isinstance(output, bytes) else output.decode().strip()
                            head = _object_id(head, "commit")
                        else:
                            sibling_head = self._repo_revision(repository, str(sibling["target_branch"]))
                            self._git_checked(["update-ref", ref, sibling_head], repository)
                            head = sibling_head
                    elif repository.is_symlink():
                        raise TeamError(409, "project_repository_conflict", "project repository already exists")
                    else:
                        created_repository = True
                        head = self._init_empty_repository(repository, target_branch)
                    now = self._now_ms()
                    conn.execute(
                        "INSERT INTO team_projects(space_id,target_branch,repository_path,head_commit,revision,created_at_ms,updated_at_ms) VALUES(?,?,?, ?,1,?,?)",
                        (space_id, target_branch, str(repository), head, now, now),
                    )
                except Exception:
                    if created_repository and repository.exists() and not repository.is_symlink():
                        shutil.rmtree(repository)
                    raise
                row = conn.execute(
                    "SELECT * FROM team_projects WHERE space_id = ? AND target_branch = ?",
                    (space_id, target_branch),
                ).fetchone()
            if row is None:
                raise TeamError(409, "project_not_ready", "project repository has not been initialized")
        return cast(sqlite3.Row, row)

    def _assert_repo_path(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.projects_root.resolve(strict=False))
        except ValueError as exc:
            raise TeamError(500, "repository_metadata_invalid", "repository metadata is invalid") from exc
        if resolved != path or path.is_symlink() or not path.is_dir():
            raise TeamError(500, "repository_metadata_invalid", "repository metadata is invalid")
        return path

    def _init_empty_repository(self, repository: Path, target_branch: str) -> str:
        repository.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._git_checked(["init", "--initial-branch", target_branch, str(repository)], self.projects_root)
        tree = self._git_checked(["mktree"], repository, input_data="").strip()
        commit = self._git_commit_tree(
            repository,
            _object_id(tree, "tree"),
            (),
            f"Initialize team project {target_branch}",
        )
        self._git_checked(["update-ref", f"refs/heads/{target_branch}", commit], repository)
        return commit

    def _init_from_source(self, source: Path, repository: Path, target_branch: str) -> str:
        repository.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._git_checked(
            ["clone", "--no-local", "--no-checkout", str(source), str(repository)],
            self.projects_root,
        )
        branch_ref = f"refs/remotes/origin/{target_branch}"
        branch_result = self._git_run(["rev-parse", "--verify", branch_ref], repository)
        if branch_result.returncode == 0:
            output = branch_result.stdout
            source_head = str(output).strip() if not isinstance(output, bytes) else output.decode().strip()
        else:
            source_head = self._git_checked(["rev-parse", "--verify", "HEAD"], repository).strip()
        source_head = _object_id(source_head, "commit")
        # The clone has a server-created config.  Remove the source remote so
        # later integration can never accidentally fetch or contact a network.
        remote = self._git_run(["remote", "remove", "origin"], repository)
        if remote.returncode != 0:
            raise TeamError(500, "git_operation_failed", "trusted repository operation failed")
        self._git_checked(["update-ref", f"refs/heads/{target_branch}", source_head], repository)
        self._materialize_commit(repository, source_head, repository)
        return source_head

    def ensure_project(
        self,
        space_id: object,
        source_repo: str | Path | None = None,
        *,
        target_branch: object = "main",
    ) -> dict[str, object]:
        """Ensure a server-owned repository for a personal or project space.

        This method deliberately has no actor parameter: it is a server
        provisioning operation.  HTTP callers must not expose it directly;
        the resulting repository path is an internal service detail.
        """

        normalized_space = _identifier(space_id, "space_id")
        branch = _branch(target_branch)
        source = self._trusted_source(source_repo) if source_repo is not None else None
        with self._lock:
            with self._db(write=True) as conn:
                self._space_row(conn, normalized_space)
                existing = conn.execute(
                    "SELECT * FROM team_projects WHERE space_id = ? AND target_branch = ?",
                    (normalized_space, branch),
                ).fetchone()
                if existing is not None:
                    repository = self._assert_repo_path(Path(str(existing["repository_path"])))
                    actual_head = self._repo_revision(repository, branch)
                    if actual_head != str(existing["head_commit"]):
                        now = self._now_ms()
                        conn.execute(
                            "UPDATE team_projects SET head_commit = ?, revision = revision + 1, updated_at_ms = ? WHERE space_id = ? AND target_branch = ?",
                            (actual_head, now, normalized_space, branch),
                        )
                        existing = conn.execute(
                            "SELECT * FROM team_projects WHERE space_id = ? AND target_branch = ?",
                            (normalized_space, branch),
                        ).fetchone()
                    assert existing is not None
                    return self._public_project(existing)

                repository = self._repository_path(normalized_space, branch)
                created_repository = False
                try:
                    if repository.exists() and not repository.is_symlink():
                        repository = self._assert_repo_path(repository)
                        sibling = conn.execute(
                            "SELECT * FROM team_projects WHERE space_id = ? ORDER BY updated_at_ms DESC, target_branch LIMIT 1",
                            (normalized_space,),
                        ).fetchone()
                        if sibling is None:
                            raise TeamError(409, "project_repository_conflict", "project repository already exists")
                        ref = f"refs/heads/{branch}"
                        branch_result = self._git_run(["rev-parse", "--verify", ref], repository)
                        if branch_result.returncode == 0:
                            output = branch_result.stdout
                            head = str(output).strip() if not isinstance(output, bytes) else output.decode().strip()
                            head = _object_id(head, "commit")
                        else:
                            sibling_branch = str(sibling["target_branch"])
                            sibling_head = self._repo_revision(repository, sibling_branch)
                            self._git_checked(["update-ref", ref, sibling_head], repository)
                            head = sibling_head
                    elif repository.is_symlink():
                        raise TeamError(409, "project_repository_conflict", "project repository already exists")
                    else:
                        created_repository = True
                        head = (
                            self._init_from_source(source, repository, branch)
                            if source is not None
                            else self._init_empty_repository(repository, branch)
                        )
                    now = self._now_ms()
                    conn.execute(
                        "INSERT INTO team_projects(space_id,target_branch,repository_path,head_commit,revision,created_at_ms,updated_at_ms) VALUES(?,?,?, ?,1,?,?)",
                        (
                            normalized_space,
                            branch,
                            str(repository),
                            head,
                            now,
                            now,
                        ),
                    )
                except Exception:
                    # A shared repository may already back another target
                    # branch; only remove it when this call initialized the
                    # object database itself.
                    if created_repository and repository.exists() and not repository.is_symlink():
                        shutil.rmtree(repository)
                    raise
                row = conn.execute(
                    "SELECT * FROM team_projects WHERE space_id = ? AND target_branch = ?",
                    (normalized_space, branch),
                ).fetchone()
                assert row is not None
                return self._public_project(row)

    @staticmethod
    def _public_project(row: sqlite3.Row) -> dict[str, object]:
        return {
            "spaceId": str(row["space_id"]),
            "targetBranch": str(row["target_branch"]),
            "repositoryPath": str(row["repository_path"]),
            "headCommit": str(row["head_commit"]),
            "revision": int(row["revision"]),
        }

    def _infer_owner(self, space_id: str) -> str:
        with self._db() as conn:
            row = self._space_row(conn, space_id)
            return str(row["owner_user_id"])

    def allocate_workspace(
        self,
        space_id: object,
        workspace_id: object,
        owner_user_id: object | None = None,
        *,
        target_branch: object = "main",
        session_id: object | None = None,
        audience: object = "owner",
        bind_session: bool = True,
        requirements_revision: object | None = None,
    ) -> dict[str, object]:
        """Create an independent checkout and optionally bind its Session.

        ``owner_user_id`` is optional for server-created workspaces and then
        resolves to the space owner.  A caller creating a Session for a
        project member should pass it explicitly.  ``bind_session=False`` is
        available to the Session service when it wants to perform the grant
        insertion itself immediately after allocation.  The optional
        ``requirements_revision`` is an internal server binding used when a
        child Session inherits its parent's frozen project brief.  HTTP
        callers do not pass this value; an omitted value captures the latest
        revision at reservation time.
        """

        normalized_space = _identifier(space_id, "space_id")
        normalized_workspace = _identifier(workspace_id, "workspace_id")
        normalized_branch = _branch(target_branch)
        owner_was_supplied = owner_user_id is not None
        session_was_supplied = session_id is not None
        normalized_owner = (
            _identifier(owner_user_id, "owner_user_id")
            if owner_user_id is not None
            else self._infer_owner(normalized_space)
        )
        normalized_session = (
            _identifier(session_id, "session_id")
            if session_id is not None
            else ""
        )
        requested_requirements_revision = (
            _requirements_revision(requirements_revision, "requirements_revision")
            if requirements_revision is not None
            else None
        )
        if not isinstance(bind_session, bool):
            raise _invalid("bind_session", "must be boolean")
        normalized_audience = _text(audience, "audience", 16).casefold()
        if normalized_audience not in {"owner", "project"}:
            raise _invalid("audience", "is unsupported")
        effective_bind_session = bool(bind_session and (owner_was_supplied or session_was_supplied))

        # This current check is intentionally repeated inside the metadata
        # transaction below.  It keeps a disabled or removed member from
        # retaining a workspace solely because it was allocated earlier.
        self.identity.require_space(normalized_owner, normalized_space, action="write")
        with self._lock:
            with self._db() as conn:
                space = self._space_row(conn, normalized_space)
                project = self._project_row(conn, normalized_space, normalized_branch)
                existing = conn.execute(
                    "SELECT * FROM team_workspaces WHERE space_id = ? AND workspace_id = ?",
                    (normalized_space, normalized_workspace),
                ).fetchone()
                workspace_path = self._workspace_path(normalized_space, normalized_workspace)
                if existing is not None:
                    if requested_requirements_revision is not None:
                        self._require_requirements_revision_conn(
                            conn,
                            normalized_space,
                            requested_requirements_revision,
                        )
                        if self._workspace_requirements_revision(existing) != requested_requirements_revision:
                            raise TeamError(
                                409,
                                "workspace_requirements_conflict",
                                "workspace is already bound to another requirements revision",
                            )
                    if (
                        str(existing["owner_user_id"]) != normalized_owner
                        or str(existing["target_branch"]) != normalized_branch
                        or str(existing["session_id"] or normalized_session) != normalized_session
                    ):
                        raise TeamError(409, "workspace_conflict", "workspace identity cannot be replaced")
                    workspace_path = Path(str(existing["workspace_path"]))
                    if workspace_path != self._workspace_path(normalized_space, normalized_workspace):
                        raise TeamError(500, "workspace_metadata_invalid", "workspace metadata is invalid")
                    if workspace_path.is_symlink() or not workspace_path.is_dir():
                        raise TeamError(409, "workspace_unavailable", "workspace is unavailable")
                    existing_result = self._public_workspace(existing)
                else:
                    existing_result = None

            # Do not call the grant store while the read connection above is
            # open: its current-state check starts its own SQLite transaction.
            if existing_result is not None:
                if effective_bind_session and normalized_session:
                    self.grants.bind_session(
                        normalized_owner,
                        normalized_space,
                        normalized_session,
                        workspace_path,
                        audience=normalized_audience,
                    )
                return existing_result

            base_commit = _object_id(str(project["head_commit"]), "base_commit")
            base_revision = int(project["revision"])
            workspace_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if workspace_path.exists() or workspace_path.is_symlink():
                raise TeamError(409, "workspace_conflict", "workspace identity cannot be replaced")
            created_workspace = False
            try:
                try:
                    workspace_path.mkdir(mode=0o700)
                except FileExistsError as exc:
                    raise TeamError(409, "workspace_conflict", "workspace identity cannot be replaced") from exc
                created_workspace = True
                self._git_checked(
                    ["clone", "--no-local", "--no-checkout", str(project["repository_path"]), str(workspace_path)],
                    self.workspaces_root,
                )
                self._materialize_commit(
                    self._assert_repo_path(Path(str(project["repository_path"]))),
                    base_commit,
                    workspace_path,
                )
                now = self._now_ms()
                with self._db(write=True) as write_conn:
                    # Re-read user/member state while reserving the
                    # immutable workspace row.  The requirements revision is
                    # captured here, alongside the reservation, so a brief
                    # published while Git materialization was in progress is
                    # the version this workspace actually starts from.
                    self._require_space_conn(write_conn, normalized_owner, normalized_space, "write")
                    if requested_requirements_revision is None:
                        requirements_revision = self._latest_requirements_revision_conn(
                            write_conn,
                            normalized_space,
                        )
                    else:
                        self._require_requirements_revision_conn(
                            write_conn,
                            normalized_space,
                            requested_requirements_revision,
                        )
                        requirements_revision = requested_requirements_revision
                    write_conn.execute(
                        "INSERT INTO team_workspaces(space_id,workspace_id,target_branch,session_id,owner_user_id,workspace_path,base_commit,base_revision,requirements_revision,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            normalized_space,
                            normalized_workspace,
                            normalized_branch,
                            normalized_session or None,
                            normalized_owner,
                            str(workspace_path.resolve(strict=True)),
                            base_commit,
                            base_revision,
                            requirements_revision,
                            now,
                        ),
                    )
                    row = write_conn.execute(
                        "SELECT * FROM team_workspaces WHERE space_id = ? AND workspace_id = ?",
                        (normalized_space, normalized_workspace),
                    ).fetchone()
                    assert row is not None
                    result = self._public_workspace(row)
                if effective_bind_session and normalized_session:
                    self.grants.bind_session(
                        normalized_owner,
                        normalized_space,
                        normalized_session,
                        workspace_path,
                        audience=normalized_audience,
                    )
                return result
            except Exception:
                # If binding failed after metadata reservation, remove only
                # this exact row; the workspace path cleanup is similarly
                # constrained to the generated server-owned directory.
                if created_workspace:
                    try:
                        with self._db(write=True) as cleanup_conn:
                            cleanup_conn.execute(
                                "DELETE FROM team_workspaces WHERE space_id = ? AND workspace_id = ? AND workspace_path = ?",
                                (normalized_space, normalized_workspace, str(workspace_path.resolve(strict=False))),
                            )
                    except Exception:
                        pass
                if created_workspace and workspace_path.exists() and not workspace_path.is_symlink():
                    shutil.rmtree(workspace_path)
                raise

    @staticmethod
    def _public_workspace(row: sqlite3.Row) -> dict[str, object]:
        result: dict[str, object] = {
            "workspaceId": str(row["workspace_id"]),
            "spaceId": str(row["space_id"]),
            "targetBranch": str(row["target_branch"]),
            "sessionId": str(row["session_id"] or ""),
            "ownerUserId": str(row["owner_user_id"]),
            "path": str(row["workspace_path"]),
            "baseCommit": str(row["base_commit"]),
            "baseRevision": int(row["base_revision"]),
            "createdAtMs": int(row["created_at_ms"]),
        }
        columns = set(row.keys())
        if "requirements_revision" in columns:
            result["requirementsRevision"] = int(row["requirements_revision"])
        if "source_draft_id" in columns and row["source_draft_id"] is not None:
            result.update(
                sourceDraftId=str(row["source_draft_id"]),
                sourceDraftCommit=str(row["source_draft_commit"]),
                sourceAdoptedAtMs=int(row["source_adopted_at_ms"]),
            )
        return result

    def attach_session(
        self,
        space_id: object,
        workspace_id: object,
        session_id: object | None = None,
        owner_user_id: object | None = None,
        *,
        audience: object = "owner",
    ) -> dict[str, object]:
        """Bind a Session created by a higher-level Session service.

        Server Session orchestration may need to allocate a checkout before it
        has generated the final ``agent:<uuid>`` Session id.  Allocation with
        omitted owner/session leaves a reserved workspace row; this method
        attaches the final id and records the parent grant atomically from the
        service's point of view.
        """

        # Also accept attach_session(workspace_id, session_id) when the
        # workspace id is globally unique in the caller's server.  The
        # explicit three-argument form remains preferred for HTTP routing.
        inferred_space = session_id is None
        if inferred_space:
            normalized_workspace = _identifier(space_id, "workspace_id")
            normalized_session = _identifier(workspace_id, "session_id")
            normalized_space = ""
        else:
            normalized_space = _identifier(space_id, "space_id")
            normalized_workspace = _identifier(workspace_id, "workspace_id")
            normalized_session = _identifier(session_id, "session_id")
        normalized_audience = _text(audience, "audience", 16).casefold()
        if normalized_audience not in {"owner", "project"}:
            raise _invalid("audience", "is unsupported")
        with self._lock:
            with self._db() as conn:
                if inferred_space:
                    candidates = conn.execute(
                        "SELECT * FROM team_workspaces WHERE workspace_id = ? ORDER BY space_id",
                        (normalized_workspace,),
                    ).fetchall()
                    if len(candidates) != 1:
                        raise _not_found("workspace not found")
                    row = candidates[0]
                    normalized_space = str(row["space_id"])
                else:
                    row = conn.execute(
                        "SELECT * FROM team_workspaces WHERE space_id = ? AND workspace_id = ?",
                        (normalized_space, normalized_workspace),
                    ).fetchone()
                if row is None:
                    raise _not_found("workspace not found")
                owner = (
                    _identifier(owner_user_id, "owner_user_id")
                    if owner_user_id is not None
                    else str(row["owner_user_id"])
                )
                if str(row["owner_user_id"]) != owner:
                    raise _denied()
                old_session = str(row["session_id"] or "")
                if old_session and old_session != normalized_session:
                    raise TeamError(409, "workspace_conflict", "workspace is already bound to another Session")
                path = self._workspace_path_is_expected(row)

            self.identity.require_space(owner, normalized_space, action="write")
            self.grants.bind_session(
                owner,
                normalized_space,
                normalized_session,
                path,
                audience=normalized_audience,
            )
            with self._db(write=True) as conn:
                current = conn.execute(
                    "SELECT * FROM team_workspaces WHERE space_id = ? AND workspace_id = ?",
                    (normalized_space, normalized_workspace),
                ).fetchone()
                if current is None or str(current["owner_user_id"]) != owner:
                    raise _denied()
                old_session = str(current["session_id"] or "")
                if old_session and old_session != normalized_session:
                    raise TeamError(409, "workspace_conflict", "workspace is already bound to another Session")
                if not old_session:
                    conn.execute(
                        "UPDATE team_workspaces SET session_id = ? WHERE space_id = ? AND workspace_id = ?",
                        (normalized_session, normalized_space, normalized_workspace),
                    )
                refreshed = conn.execute(
                    "SELECT * FROM team_workspaces WHERE space_id = ? AND workspace_id = ?",
                    (normalized_space, normalized_workspace),
                ).fetchone()
                assert refreshed is not None
                return self._public_workspace(refreshed)

    def accept_requirements(
        self,
        session_id: object,
        actor_user_id: object,
        *,
        base_revision: int,
        revision: int,
    ) -> dict[str, object]:
        """Advance one Session workspace to a current project brief revision.

        The caller supplies the workspace's observed requirements revision as
        ``base_revision`` and the exact currently published brief as
        ``revision``.  Both the active grant and the latest brief are checked
        again after the worker is quiesced, so a concurrent membership or brief
        change cannot silently move a Session's baseline.
        """

        normalized_session = _identifier(session_id, "session_id")
        actor = _identifier(actor_user_id, "actor_user_id")
        expected_revision = _requirements_revision(base_revision, "base_revision")
        requested_revision = _requirements_revision(revision, "revision")
        binding = self.grants.binding(normalized_session, check_current=True)
        if str(binding["ownerUserId"]) != actor:
            raise _not_found("session not found in this space")
        space_id = str(binding["spaceId"])
        with self._lock:
            with self._db() as conn:
                current_binding = self._current_binding_conn(conn, normalized_session)
                if (
                    str(current_binding["space_id"]) != space_id
                    or str(current_binding["owner_user_id"]) != actor
                ):
                    raise _denied()
                self._require_space_conn(conn, actor, space_id, "write")
                current_requirements = self._latest_requirements_revision_conn(conn, space_id)
                if current_requirements == 0:
                    raise TeamError(
                        409,
                        "requirements_not_published",
                        "a published project brief is required before accepting requirements",
                    )
                if requested_revision != current_requirements:
                    raise TeamError(
                        409,
                        "stale_requirements",
                        "requested requirements revision is not current",
                    )
                workspace = self._workspace_for_session(conn, normalized_session)
                if str(workspace["space_id"]) != space_id or str(workspace["owner_user_id"]) != actor:
                    raise _denied()
                previous_revision = self._workspace_requirements_revision(workspace)
                workspace_path = self._workspace_path_is_expected(workspace)
                # Replaying an already-applied acceptance is a read-only,
                # idempotent operation.  In particular, do not quiesce a
                # worker that was started after the first acceptance merely
                # because the client still sends its older base revision.
                if previous_revision == requested_revision:
                    return {
                        "sessionId": normalized_session,
                        "requirementsRevision": requested_revision,
                        "previousRequirementsRevision": previous_revision,
                    }

            # A worker may be running with the old requirements. Freeze it
            # before taking the authoritative metadata transaction below.
            with self._quiesced(normalized_session, workspace_path):
                with self._db(write=True) as conn:
                    current_binding = self._current_binding_conn(conn, normalized_session)
                    if (
                        str(current_binding["space_id"]) != space_id
                        or str(current_binding["owner_user_id"]) != actor
                    ):
                        raise _denied()
                    self._require_space_conn(conn, actor, space_id, "write")
                    current_requirements = self._latest_requirements_revision_conn(conn, space_id)
                    if current_requirements == 0:
                        raise TeamError(
                            409,
                            "requirements_not_published",
                            "a published project brief is required before accepting requirements",
                        )
                    if requested_revision != current_requirements:
                        raise TeamError(
                            409,
                            "stale_requirements",
                            "requested requirements revision is not current",
                        )
                    current_workspace = self._workspace_for_session(conn, normalized_session)
                    previous_revision = self._workspace_requirements_revision(current_workspace)
                    if expected_revision != previous_revision:
                        raise TeamError(
                            409,
                            "workspace_requirements_conflict",
                            "workspace requirements changed; reload before accepting",
                        )
                    if previous_revision != requested_revision:
                        conn.execute(
                            "UPDATE team_workspaces SET requirements_revision = ? WHERE space_id = ? AND workspace_id = ?",
                            (requested_revision, space_id, str(current_workspace["workspace_id"])),
                        )
                    return {
                        "sessionId": normalized_session,
                        "requirementsRevision": requested_revision,
                        "previousRequirementsRevision": previous_revision,
                    }

    def requirements_for_session(self, session_id: object) -> dict[str, object]:
        """Read the immutable brief snapshot bound to a current Session.

        This is an internal runtime adapter.  The caller must still authorize
        the human request before exposing its result; this method itself only
        resolves a current grant and its matching workspace row.  It never
        creates a repository or starts a runtime.
        """

        normalized_session = _identifier(session_id, "session_id")
        binding = self.grants.binding(normalized_session, check_current=True)
        space_id = str(binding["spaceId"])
        with self._lock:
            with self._db() as conn:
                workspace = self._workspace_for_session(conn, normalized_session)
                if (
                    str(workspace["space_id"]) != space_id
                    or str(workspace["session_id"] or "") != normalized_session
                    or str(workspace["owner_user_id"]) != str(binding["ownerUserId"])
                ):
                    raise _denied()
                self._workspace_path_is_expected(workspace)
                requirements_revision = self._workspace_requirements_revision(workspace)
                current_requirements_revision = self._latest_requirements_revision_conn(conn, space_id)
                brief = None
                if requirements_revision:
                    brief = conn.execute(
                        "SELECT * FROM team_project_briefs WHERE space_id = ? AND revision = ?",
                        (space_id, requirements_revision),
                    ).fetchone()
                    if brief is None:
                        raise TeamError(500, "requirements_metadata_invalid", "workspace requirements revision is unavailable")
                return {
                    "spaceId": space_id,
                    "requirementsRevision": requirements_revision,
                    "currentRequirementsRevision": current_requirements_revision,
                    "objective": str(brief["objective"]) if brief is not None else "",
                    "acceptanceCriteria": (
                        [item for item in _json_list(brief["acceptance_criteria_json"]) if isinstance(item, str)]
                        if brief is not None
                        else []
                    ),
                }

    def _open_beneath_data_root(self, path: Path) -> int:
        try:
            relative = path.relative_to(self.data_root)
        except ValueError as exc:
            raise TeamError(400, "unsafe_workspace", "workspace is outside the team root") from exc
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(self.data_root), flags)
            for component in relative.parts:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except (OSError, ValueError) as exc:
            try:
                os.close(descriptor)
            except (UnboundLocalError, OSError):
                pass
            raise TeamError(400, "unsafe_workspace", "workspace is unavailable or unsafe") from exc

    def _snapshot_workspace(
        self,
        workspace: Path,
        stage_root: Path,
        *,
        reject_private: bool = False,
    ) -> tuple[list[dict[str, object]], str, list[str], bool]:
        """Capture a workspace and report bounded default exclusions.

        ``excluded_paths`` records explicit metadata/credential paths skipped
        by the delivery safeguard.  It is intentionally bounded and does not
        claim to detect every secret.  Generic source directories such as
        ``runtime``, ``sessions``, ``logs``, and ``private`` remain publishable;
        symlinks and unsupported file types still fail closed.
        """

        root_fd = self._open_beneath_data_root(workspace)
        files: list[tuple[str, bytes, int]] = []
        manifest: list[dict[str, object]] = []
        excluded_paths: list[str] = []
        excluded_paths_truncated = False
        total_bytes = 0
        try:
            def visit(directory_fd: int, prefix: str) -> None:
                nonlocal excluded_paths_truncated, total_bytes
                try:
                    iterator = os.scandir(directory_fd)
                except OSError as exc:
                    raise TeamError(400, "unsafe_workspace", "workspace cannot be read safely") from exc
                with iterator:
                    for entry in iterator:
                        name = entry.name
                        if name == ".git":
                            # It belongs to the Agent and is never part of a
                            # delivered snapshot.
                            continue
                        relative = _safe_relative_path(
                            f"{prefix}/{name}" if prefix else name,
                            "workspace path",
                        )
                        try:
                            metadata = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            raise TeamError(400, "unsafe_workspace", "workspace cannot be read safely") from exc
                        mode = metadata.st_mode
                        if stat.S_ISLNK(mode):
                            raise TeamError(400, "unsafe_workspace", "workspace symlinks are not publishable")
                        if _is_private_path(relative):
                            if reject_private:
                                raise TeamError(
                                    409,
                                    "workspace_dirty",
                                    "target workspace contains private or runtime files; publish or discard them before adopting a draft",
                                )
                            if len(excluded_paths) < _MAX_EXCLUDED_PATHS:
                                excluded_paths.append(relative[:_MAX_EXCLUDED_PATH_LENGTH])
                            else:
                                excluded_paths_truncated = True
                            continue
                        if stat.S_ISDIR(mode):
                            try:
                                child_fd = os.open(
                                    name,
                                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                                    dir_fd=directory_fd,
                                )
                            except OSError as exc:
                                raise TeamError(400, "unsafe_workspace", "workspace cannot be read safely") from exc
                            try:
                                visit(child_fd, relative)
                            finally:
                                os.close(child_fd)
                            continue
                        if not stat.S_ISREG(mode):
                            raise TeamError(400, "unsafe_workspace", "workspace contains an unsupported file")
                        if metadata.st_size > _MAX_FILE_BYTES:
                            raise TeamError(400, "workspace_too_large", "workspace file is too large")
                        if len(files) >= _MAX_WORKSPACE_FILES:
                            raise TeamError(400, "workspace_too_large", "workspace contains too many files")
                        try:
                            file_fd = os.open(
                                name,
                                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                dir_fd=directory_fd,
                            )
                            with os.fdopen(file_fd, "rb", closefd=True) as handle:
                                content = handle.read(_MAX_FILE_BYTES + 1)
                        except OSError as exc:
                            raise TeamError(400, "unsafe_workspace", "workspace cannot be read safely") from exc
                        if len(content) > _MAX_FILE_BYTES:
                            raise TeamError(400, "workspace_too_large", "workspace file is too large")
                        try:
                            after = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            raise TeamError(409, "workspace_changed", "workspace changed during snapshot") from exc
                        if after.st_ino != metadata.st_ino or after.st_size != metadata.st_size or after.st_mtime_ns != metadata.st_mtime_ns:
                            raise TeamError(409, "workspace_changed", "workspace changed during snapshot")
                        total_bytes += len(content)
                        if total_bytes > _MAX_SNAPSHOT_BYTES:
                            raise TeamError(400, "workspace_too_large", "workspace snapshot is too large")
                        digest = hashlib.sha256(content).hexdigest()
                        permission = stat.S_IMODE(metadata.st_mode)
                        files.append((relative, content, permission))
                        manifest.append(
                            {
                                "path": relative,
                                "size": len(content),
                                "sha256": digest,
                                "mode": permission,
                            }
                        )

            visit(root_fd, "")
        finally:
            os.close(root_fd)

        manifest.sort(key=lambda item: str(item["path"]))
        files.sort(key=lambda item: item[0])
        stage_root.mkdir(mode=0o700, parents=False, exist_ok=False)
        self._write_stage_files(stage_root, files)
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return (
            manifest,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            excluded_paths,
            excluded_paths_truncated,
        )

    def _write_stage_files(self, stage_root: Path, files: Sequence[tuple[str, bytes, int]]) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(str(stage_root), flags)
        try:
            for relative, content, permission in files:
                parts = relative.split("/")
                directory_fd = root_fd
                opened: list[int] = []
                try:
                    for part in parts[:-1]:
                        try:
                            os.mkdir(part, 0o700, dir_fd=directory_fd)
                        except FileExistsError:
                            pass
                        child_fd = os.open(part, flags, dir_fd=directory_fd)
                        opened.append(child_fd)
                        directory_fd = child_fd
                    file_fd = os.open(
                        parts[-1],
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        permission & 0o777,
                        dir_fd=directory_fd,
                    )
                    try:
                        view = memoryview(content)
                        while view:
                            written = os.write(file_fd, view)
                            view = view[written:]
                        os.fchmod(file_fd, permission & 0o777)
                    finally:
                        os.close(file_fd)
                finally:
                    for descriptor in reversed(opened):
                        os.close(descriptor)
        finally:
            os.close(root_fd)

    def _write_relative_file(self, root: Path, relative: str, content: bytes, permission: int) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(str(root), flags)
        opened: list[int] = []
        try:
            parts = relative.split("/")
            directory_fd = root_fd
            for part in parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(part, flags, dir_fd=directory_fd)
                opened.append(child_fd)
                directory_fd = child_fd
            file_fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                permission & 0o777,
                dir_fd=directory_fd,
            )
            try:
                view = memoryview(content)
                while view:
                    written = os.write(file_fd, view)
                    view = view[written:]
                os.fchmod(file_fd, permission & 0o777)
            finally:
                os.close(file_fd)
        except OSError as exc:
            raise TeamError(500, "workspace_materialization_failed", "workspace materialization failed") from exc
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
            os.close(root_fd)

    def _materialize_commit(self, repository: Path, commit: str, destination: Path) -> None:
        if not self._git_checked(["ls-tree", "-r", "--name-only", commit], repository).strip():
            return
        archive = self._git_bytes(["archive", "--format=tar", commit], repository)
        if len(archive) > _MAX_ARCHIVE_BYTES:
            raise TeamError(400, "workspace_too_large", "repository snapshot is too large")
        try:
            tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:")
        except (tarfile.TarError, OSError) as exc:
            raise TeamError(500, "repository_corrupt", "repository snapshot is invalid") from exc
        with tar:
            for member in tar:
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise TeamError(400, "unsafe_workspace", "repository symlinks are not publishable")
                relative = _safe_relative_path(member.name)
                if relative == ".git" or relative.startswith(".git/"):
                    raise TeamError(500, "repository_corrupt", "repository contains forbidden metadata")
                if member.isdir():
                    # Directory creation is performed when its first file is
                    # written, preserving the same no-follow path discipline.
                    continue
                if member.size > _MAX_FILE_BYTES:
                    raise TeamError(400, "workspace_too_large", "repository file is too large")
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise TeamError(500, "repository_corrupt", "repository snapshot is invalid")
                content = extracted.read(_MAX_FILE_BYTES + 1)
                if len(content) > _MAX_FILE_BYTES:
                    raise TeamError(400, "workspace_too_large", "repository file is too large")
                self._write_relative_file(destination, relative, content, stat.S_IMODE(member.mode))

    def _set_candidate_read_only(self, root: Path) -> None:
        """Make a materialized verifier source tree read-only.

        The containing candidate directory remains writable for verifier
        scratch files.  Materialized source entries are regular files and
        directories produced by ``_materialize_commit``; reject anything else
        before handing the path to an injected verifier.
        """

        if root.is_symlink() or not root.is_dir():
            raise TeamError(500, "unsafe_workspace", "candidate source is not a directory")
        for current, directories, files in os.walk(root, topdown=False, followlinks=False):
            for name in (*files, *directories):
                path = Path(current) / name
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise TeamError(500, "unsafe_workspace", "candidate source cannot be inspected") from exc
                if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                    raise TeamError(400, "unsafe_workspace", "candidate source contains an unsupported entry")
                permission = stat.S_IMODE(metadata.st_mode) & ~0o222
                if stat.S_ISDIR(metadata.st_mode):
                    permission |= 0o500
                else:
                    permission |= 0o400
                try:
                    os.chmod(path, permission)
                except OSError as exc:
                    raise TeamError(500, "unsafe_workspace", "candidate source cannot be protected") from exc
        try:
            metadata = root.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise TeamError(500, "unsafe_workspace", "candidate source is not a directory")
            os.chmod(root, (stat.S_IMODE(metadata.st_mode) & ~0o222) | 0o500)
        except OSError as exc:
            raise TeamError(500, "unsafe_workspace", "candidate source cannot be protected") from exc

    def _remove_candidate_stage(self, root: Path) -> None:
        """Restore removal permissions and delete verifier staging safely."""

        if not root.exists() or root.is_symlink():
            return
        for current, directories, files in os.walk(root, topdown=False, followlinks=False):
            for name in (*files, *directories):
                path = Path(current) / name
                try:
                    metadata = path.lstat()
                    if stat.S_ISLNK(metadata.st_mode):
                        path.unlink()
                    elif stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode):
                        os.chmod(path, stat.S_IMODE(metadata.st_mode) | 0o700)
                except OSError:
                    pass
        try:
            os.chmod(root, stat.S_IMODE(root.lstat().st_mode) | 0o700)
        except OSError:
            pass
        shutil.rmtree(root, ignore_errors=True)

    def _replace_repository_worktree(self, repository: Path, commit: str) -> None:
        """Refresh a trusted repository's visible tree without Git checkout.

        The repository directory is server-owned, but the tree may contain
        untrusted filenames and attributes.  Clearing and writing it through
        the archive path avoids checkout filters and hooks while keeping the
        path useful to server-side project readers.
        """

        with os.scandir(repository) as entries:
            for entry in entries:
                if entry.name == ".git":
                    continue
                child = repository / entry.name
                if entry.is_symlink():
                    child.unlink()
                elif entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(child)
                else:
                    child.unlink()
        self._materialize_commit(repository, commit, repository)

    def _clear_workspace_worktree(self, workspace: Path) -> None:
        """Remove every agent file while preserving its private ``.git`` dir."""

        root_fd = self._open_beneath_data_root(workspace)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            def clear(directory_fd: int) -> None:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        if entry.name == ".git":
                            continue
                        metadata = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(metadata.st_mode):
                            raise TeamError(400, "unsafe_workspace", "workspace symlinks are not publishable")
                        if stat.S_ISDIR(metadata.st_mode):
                            child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                            try:
                                clear(child_fd)
                            finally:
                                os.close(child_fd)
                            os.rmdir(entry.name, dir_fd=directory_fd)
                        elif stat.S_ISREG(metadata.st_mode):
                            os.unlink(entry.name, dir_fd=directory_fd)
                        else:
                            raise TeamError(400, "unsafe_workspace", "workspace contains an unsupported file")

            clear(root_fd)
        except OSError as exc:
            raise TeamError(400, "unsafe_workspace", "workspace could not be updated safely") from exc
        finally:
            os.close(root_fd)

    def _git_commit_tree(
        self,
        repository: Path,
        tree: str,
        parents: Sequence[str],
        message: str,
    ) -> str:
        args = ["commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        args.extend(["-F", "-"])
        now = self._now_ms()
        result = self._git_checked(
            args,
            repository,
            input_data=message,
            extra_env={
                "GIT_AUTHOR_NAME": "PAW Team Server",
                "GIT_AUTHOR_EMAIL": "paw-team@localhost",
                "GIT_COMMITTER_NAME": "PAW Team Server",
                "GIT_COMMITTER_EMAIL": "paw-team@localhost",
                "GIT_AUTHOR_DATE": f"@{now // 1000} +0000",
                "GIT_COMMITTER_DATE": f"@{now // 1000} +0000",
            },
        ).strip()
        return _object_id(result, "commit")

    def _build_draft_commit(
        self,
        repository: Path,
        base_commit: str,
        stage_root: Path,
        manifest: Sequence[Mapping[str, object]],
        draft_id: str,
        description: str,
    ) -> str:
        index_path = self.staging_root / f"index-{draft_id}"
        manifest_paths = {str(item["path"]) for item in manifest}
        extra_env = {"GIT_INDEX_FILE": str(index_path)}
        try:
            self._git_checked(["read-tree", base_commit], repository, extra_env=extra_env)
            base_paths_raw = self._git_run(
                ["ls-tree", "-r", "-z", "--name-only", base_commit],
                repository,
            ).stdout
            base_paths_bytes = base_paths_raw if isinstance(base_paths_raw, bytes) else str(base_paths_raw).encode()
            for raw_path in base_paths_bytes.split(b"\0"):
                if not raw_path:
                    continue
                try:
                    base_path = _safe_relative_path(raw_path.decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise TeamError(400, "unsafe_workspace", "repository path is not valid UTF-8") from exc
                if base_path not in manifest_paths:
                    self._git_checked(
                        ["update-index", "--remove", "--ignore-unmatch", "--", base_path],
                        repository,
                        extra_env=extra_env,
                    )
            for item in manifest:
                relative = _safe_relative_path(item["path"])
                staged = stage_root.joinpath(*relative.split("/"))
                if not staged.is_file() or staged.is_symlink():
                    raise TeamError(400, "unsafe_workspace", "workspace snapshot is invalid")
                blob = self._git_checked(
                    ["hash-object", "-w", "--no-filters", str(staged)],
                    repository,
                ).strip()
                mode = "100755" if int(cast(Any, item.get("mode", 0))) & 0o111 else "100644"
                self._git_checked(
                    ["update-index", "--add", "--cacheinfo", f"{mode},{_object_id(blob, 'blob')},{relative}"],
                    repository,
                    extra_env=extra_env,
                )
            tree = _object_id(self._git_checked(["write-tree"], repository, extra_env=extra_env).strip(), "tree")
            return self._git_commit_tree(
                repository,
                tree,
                (base_commit,),
                f"Draft {draft_id}: {description}",
            )
        finally:
            try:
                index_path.unlink()
            except FileNotFoundError:
                pass

    def _quiesced(self, session_id: str, workspace: Path) -> Any:
        if self.quiesce is None:
            return nullcontext()
        try:
            value = _call_compatible(
                self.quiesce,
                ((session_id, workspace), (session_id,), ()),
            )
        except Exception as exc:
            raise TeamError(503, "quiesce_failed", "workspace could not be frozen") from exc
        if value is None:
            return nullcontext()
        if not hasattr(value, "__enter__") or not hasattr(value, "__exit__"):
            raise TeamError(500, "quiesce_invalid", "quiesce callback must return a context manager")
        return cast(Any, value)

    def _workspace_for_session(self, conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM team_workspaces WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise _denied()
        return cast(sqlite3.Row, row)

    def _require_space_conn(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        space_id: str,
        action: str,
    ) -> dict[str, object]:
        user = conn.execute("SELECT * FROM team_users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise TeamError(401, "authentication_required", "authentication is required")
        if not bool(user["active"]):
            raise TeamError(401, "account_disabled", "account is disabled")
        space = conn.execute("SELECT * FROM team_spaces WHERE id = ?", (space_id,)).fetchone()
        if space is None:
            raise _space_not_found()
        kind = str(space["kind"])
        if kind == "personal":
            if str(space["owner_user_id"]) != user_id:
                raise _space_not_found()
            role = "owner"
            membership_revision = int(user["authorization_revision"])
        else:
            member = conn.execute(
                "SELECT role, active, membership_revision FROM team_project_members WHERE space_id = ? AND user_id = ?",
                (space_id, user_id),
            ).fetchone()
            if member is None or not bool(member["active"]):
                raise _space_not_found()
            role = str(member["role"])
            membership_revision = int(member["membership_revision"])
        if action == "write" and role not in _WRITE_ROLES:
            raise TeamError(403, "forbidden", "project write access is required")
        if action == "manage" and role not in _MANAGE_ROLES:
            raise TeamError(403, "forbidden", "project manage access is required")
        return {
            "id": str(space["id"]),
            "kind": kind,
            "name": str(space["name"]),
            "role": role,
            "revision": int(space["revision"]),
            "membershipRevision": membership_revision,
        }

    @staticmethod
    def _latest_requirements_revision_conn(
        conn: sqlite3.Connection,
        space_id: str,
    ) -> int:
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
                "requirements revision is not available for this space",
            )

    @staticmethod
    def _workspace_requirements_revision(row: sqlite3.Row) -> int:
        if "requirements_revision" not in row.keys():
            return 0
        return int(row["requirements_revision"])

    def _current_binding_conn(self, conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM team_session_bindings WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None or not bool(row["active"]):
            raise _denied()
        scope = self._require_space_conn(
            conn,
            str(row["owner_user_id"]),
            str(row["space_id"]),
            "write",
        )
        if int(row["membership_revision"]) != int(cast(Any, scope["membershipRevision"])):
            raise _denied()
        return cast(sqlite3.Row, row)

    def _workspace_path_is_expected(self, row: sqlite3.Row) -> Path:
        expected = self._workspace_path(str(row["space_id"]), str(row["workspace_id"]))
        actual = Path(str(row["workspace_path"]))
        if actual != expected or actual.is_symlink() or not actual.is_dir():
            raise TeamError(400, "unsafe_workspace", "workspace is outside the managed root")
        return actual

    def share_draft(
        self,
        session_id: object,
        actor_user_id: object,
        description: object,
    ) -> dict[str, object]:
        normalized_session = _identifier(session_id, "session_id")
        actor = _identifier(actor_user_id, "actor_user_id")
        normalized_description = _text(description, "description", _MAX_DESCRIPTION)
        binding = self.grants.binding(normalized_session, check_current=True)
        if str(binding["ownerUserId"]) != actor:
            # Avoid revealing whether another user's Session exists.
            raise _not_found("session not found in this space")
        space_id = str(binding["spaceId"])
        self.identity.require_space(actor, space_id, action="write")
        with self._lock:
            with self._db() as conn:
                space = self._require_space_conn(conn, actor, space_id, "write")
                if space["kind"] != "project":
                    raise TeamError(403, "project_delivery_forbidden", "personal workspaces cannot publish shared drafts")
                workspace = conn.execute(
                    "SELECT * FROM team_workspaces WHERE session_id = ?",
                    (normalized_session,),
                ).fetchone()
                if workspace is None:
                    # Session orchestration may bind the final id after
                    # allocation.  The grant's descriptor-rooted path is the
                    # authoritative fallback for that short handoff window.
                    workspace = conn.execute(
                        "SELECT * FROM team_workspaces WHERE space_id = ? AND workspace_path = ?",
                        (space_id, str(binding["workspacePath"])),
                    ).fetchone()
                if workspace is None:
                    raise _denied()
                if str(workspace["owner_user_id"]) != actor:
                    raise _denied()
                workspace_path = self._workspace_path_is_expected(workspace)
                project = self._project_row(conn, space_id, str(workspace["target_branch"]))
                repository = self._assert_repo_path(Path(str(project["repository_path"])))
                base_commit = _object_id(str(workspace["base_commit"]), "base_commit")
                requirements_revision = self._workspace_requirements_revision(workspace)
                draft_id = _new_id("draft")
                stage_root = self.staging_root / f"snapshot-{draft_id}"
                try:
                    # The callback owns the actual Session pause/resume; the
                    # complete read and fixed commit are inside its boundary.
                    with self._quiesced(normalized_session, workspace_path):
                        (
                            manifest,
                            manifest_sha256,
                            excluded_paths,
                            excluded_paths_truncated,
                        ) = self._snapshot_workspace(workspace_path, stage_root)
                        draft_commit = self._build_draft_commit(
                            repository,
                            base_commit,
                            stage_root,
                            manifest,
                            draft_id,
                            normalized_description,
                        )
                finally:
                    if stage_root.exists() and not stage_root.is_symlink():
                        shutil.rmtree(stage_root)

                canonical_manifest = json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                now = self._now_ms()
                with self._db(write=True) as write_conn:
                    # Re-read current actor, binding, and membership revision
                    # after the potentially long filesystem snapshot.
                    current_scope = self._require_space_conn(write_conn, actor, space_id, "write")
                    current_binding = self._current_binding_conn(write_conn, normalized_session)
                    if (
                        str(current_binding["owner_user_id"]) != actor
                        or int(current_binding["membership_revision"]) != int(binding["membershipRevision"])
                    ):
                        raise _denied()
                    current_workspace = self._workspace_for_session(write_conn, normalized_session)
                    if str(current_workspace["base_commit"]) != base_commit:
                        raise TeamError(409, "workspace_changed", "workspace base changed during snapshot")
                    if self._workspace_requirements_revision(current_workspace) != requirements_revision:
                        raise TeamError(
                            409,
                            "workspace_changed",
                            "workspace requirements changed during snapshot",
                        )
                    write_conn.execute(
                        "INSERT INTO team_drafts(id,space_id,target_branch,workspace_id,session_id,owner_user_id,description,base_commit,base_revision,requirements_revision,draft_commit,manifest_json,manifest_sha256,status,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            draft_id,
                            space_id,
                            str(workspace["target_branch"]),
                            str(workspace["workspace_id"]),
                            normalized_session,
                            actor,
                            normalized_description,
                            base_commit,
                            int(workspace["base_revision"]),
                            requirements_revision,
                            draft_commit,
                            canonical_manifest,
                            manifest_sha256,
                            "pending",
                            now,
                        ),
                    )
                    write_conn.execute(
                        "UPDATE team_drafts SET result_json = ? WHERE id = ?",
                        (
                            _safe_json(
                                {
                                    "excludedPaths": excluded_paths,
                                    "excludedPathsTruncated": excluded_paths_truncated,
                                }
                            ),
                            draft_id,
                        ),
                    )
                    row = write_conn.execute("SELECT * FROM team_drafts WHERE id = ?", (draft_id,)).fetchone()
                    assert row is not None
                    return self._public_draft(row)

    def _draft_delivery_metadata(self, row: sqlite3.Row) -> dict[str, object]:
        if not row["result_json"]:
            return {}
        metadata = _load_json_object(row["result_json"])
        raw_paths = metadata.get("excludedPaths")
        if not isinstance(raw_paths, list):
            return {}
        paths = [
            item[:_MAX_EXCLUDED_PATH_LENGTH]
            for item in raw_paths[:_MAX_EXCLUDED_PATHS]
            if isinstance(item, str)
        ]
        return {
            "excludedPaths": paths,
            "excludedPathsTruncated": bool(metadata.get("excludedPathsTruncated")),
        }

    def _with_draft_delivery_metadata(
        self,
        row: sqlite3.Row,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        enriched = dict(result)
        for key, value in self._draft_delivery_metadata(row).items():
            enriched.setdefault(key, value)
        return enriched

    def _public_draft(self, row: sqlite3.Row) -> dict[str, object]:
        manifest = json.loads(str(row["manifest_json"]))
        if not isinstance(manifest, list):
            raise TeamError(500, "draft_metadata_invalid", "draft metadata is invalid")
        result: dict[str, object] = {
            "draftId": str(row["id"]),
            "spaceId": str(row["space_id"]),
            "targetBranch": str(row["target_branch"]),
            "workspaceId": str(row["workspace_id"]),
            "sessionId": str(row["session_id"]),
            "ownerUserId": str(row["owner_user_id"]),
            "description": str(row["description"]),
            "baseCommit": str(row["base_commit"]),
            "baseRevision": int(row["base_revision"]),
            "draftCommit": str(row["draft_commit"]),
            "manifest": manifest,
            "manifestSha256": str(row["manifest_sha256"]),
            "status": str(row["status"]),
            "createdAtMs": int(row["created_at_ms"]),
        }
        if "requirements_revision" in row.keys():
            result["requirementsRevision"] = int(row["requirements_revision"])
        result.update(self._draft_delivery_metadata(row))
        if row["integrated_at_ms"] is not None:
            result["integratedAtMs"] = int(row["integrated_at_ms"])
        if row["integrated_commit"] is not None:
            result["integratedCommit"] = str(row["integrated_commit"])
        return result

    def list_drafts(
        self,
        space_id: object,
        actor_user_id: object,
        target_branch: object = "main",
    ) -> list[dict[str, object]]:
        normalized_space = _identifier(space_id, "space_id")
        actor = _identifier(actor_user_id, "actor_user_id")
        branch = _branch(target_branch)
        with self._db() as conn:
            scope = self._require_space_conn(conn, actor, normalized_space, "read")
            if scope["kind"] != "project":
                raise TeamError(403, "project_delivery_forbidden", "personal workspaces have no shared drafts")
            rows = conn.execute(
                "SELECT * FROM team_drafts WHERE space_id = ? AND target_branch = ? ORDER BY created_at_ms, id",
                (normalized_space, branch),
            ).fetchall()
            return [self._public_draft(row) for row in rows]

    def read_draft(
        self,
        space_id: object,
        draft_id: object,
        actor_user_id: object | None = None,
    ) -> dict[str, object]:
        # A two-argument compatibility form, read_draft(draft_id, actor), is
        # useful to small internal callers while the three-argument form keeps
        # the space scope explicit for HTTP routes.
        if actor_user_id is None:
            actor_user_id = draft_id
            draft_id = space_id
            space_id = None
        normalized_draft = _identifier(draft_id, "draft_id")
        actor = _identifier(actor_user_id, "actor_user_id")
        with self._db() as conn:
            row = conn.execute("SELECT * FROM team_drafts WHERE id = ?", (normalized_draft,)).fetchone()
            if row is None:
                raise _not_found()
            actual_space = str(row["space_id"])
            if space_id is not None and _identifier(space_id, "space_id") != actual_space:
                raise _not_found()
            scope = self._require_space_conn(conn, actor, actual_space, "read")
            if scope["kind"] != "project":
                raise _not_found()
            return self._public_draft(row)

    def _adoption_result(self, raw: object) -> dict[str, object]:
        result = _load_json_object(raw)
        if not result:
            raise TeamError(500, "adoption_metadata_invalid", "workspace adoption metadata is invalid")
        return result

    def _record_adoption(
        self,
        *,
        adoption_id: str,
        space_id: str,
        workspace_id: str,
        session_id: str,
        draft_id: str,
        draft_commit: str,
        base_commit: str,
        target_tree_commit: str,
        status: str,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        encoded = _safe_json(dict(result))
        with self._db(write=True) as conn:
            existing = conn.execute(
                "SELECT result_json FROM team_workspace_adoptions WHERE workspace_id = ? AND draft_id = ?",
                (workspace_id, draft_id),
            ).fetchone()
            if existing is not None:
                return self._adoption_result(existing["result_json"])
            now = self._now_ms()
            conn.execute(
                "INSERT INTO team_workspace_adoptions(id,space_id,workspace_id,session_id,draft_id,draft_commit,base_commit,target_tree_commit,status,result_json,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    adoption_id,
                    space_id,
                    workspace_id,
                    session_id,
                    draft_id,
                    draft_commit,
                    base_commit,
                    target_tree_commit,
                    status,
                    encoded,
                    now,
                ),
            )
        return dict(result)

    def adopt_draft(
        self,
        space_id: object,
        draft_id: object,
        actor_user_id: object,
        target_session_id: object,
    ) -> dict[str, object]:
        """Apply a fixed draft to a pristine target Session workspace.

        The target is first captured as a commit whose parent is its recorded
        base.  Git's three-way merge then uses the draft's fixed base as the
        explicit merge base.  Any target edits cause an early ``workspace_dirty``
        error, and merge conflicts are recorded without touching the target.
        """

        normalized_space = _identifier(space_id, "space_id")
        normalized_draft = _identifier(draft_id, "draft_id")
        actor = _identifier(actor_user_id, "actor_user_id")
        target_session = _identifier(target_session_id, "target_session_id")
        initial_binding = self.grants.require_session(
            actor,
            normalized_space,
            target_session,
            action="write",
        )
        with self._lock:
            with self._db() as conn:
                scope = self._require_space_conn(conn, actor, normalized_space, "write")
                if scope["kind"] != "project":
                    raise TeamError(403, "project_delivery_forbidden", "personal workspaces cannot adopt shared drafts")
                draft = conn.execute(
                    "SELECT * FROM team_drafts WHERE id = ? AND space_id = ?",
                    (normalized_draft, normalized_space),
                ).fetchone()
                if draft is None:
                    raise _not_found()
                target_workspace = conn.execute(
                    "SELECT * FROM team_workspaces WHERE session_id = ? AND space_id = ?",
                    (target_session, normalized_space),
                ).fetchone()
                if target_workspace is None:
                    raise _denied()
                target_path = self._workspace_path_is_expected(target_workspace)
                if str(initial_binding["workspacePath"]) != str(target_path):
                    raise _denied()
                if str(target_workspace["target_branch"]) != str(draft["target_branch"]):
                    raise TeamError(409, "target_branch_conflict", "draft belongs to another target branch")
                existing = conn.execute(
                    "SELECT result_json FROM team_workspace_adoptions WHERE workspace_id = ? AND draft_id = ?",
                    (str(target_workspace["workspace_id"]), normalized_draft),
                ).fetchone()
                if existing is not None:
                    return self._adoption_result(existing["result_json"])
                project = self._project_row(conn, normalized_space, str(target_workspace["target_branch"]))
                repository = self._assert_repo_path(Path(str(project["repository_path"])))
                source_base = _object_id(str(draft["base_commit"]), "base_commit")
                target_base = _object_id(str(target_workspace["base_commit"]), "base_commit")

            adoption_id = _new_id("adopt")
            snapshot_stage = self.staging_root / f"adoption-snapshot-{adoption_id}"
            target_snapshot: str | None = None
            try:
                with self._quiesced(target_session, target_path):
                    target_manifest, _target_manifest_hash, _excluded_paths, _excluded_paths_truncated = self._snapshot_workspace(
                        target_path,
                        snapshot_stage,
                        reject_private=True,
                    )
                    target_snapshot = self._build_draft_commit(
                        repository,
                        target_base,
                        snapshot_stage,
                        target_manifest,
                        adoption_id,
                        "adoption target snapshot",
                    )
                    dirty_probe = self._git_run(
                        ["diff-tree", "--quiet", target_base, target_snapshot],
                        repository,
                    )
                    if dirty_probe.returncode not in (0, 1):
                        raise TeamError(500, "git_operation_failed", "trusted repository comparison failed")
                    dirty = dirty_probe.returncode == 1
                    if dirty:
                        raise TeamError(
                            409,
                            "workspace_dirty",
                            "target workspace has uncommitted changes; publish or discard them before adopting a draft",
                        )
                    ancestor = self._git_run(
                        ["merge-base", "--is-ancestor", source_base, target_base],
                        repository,
                    )
                    if ancestor.returncode not in (0, 1):
                        raise TeamError(500, "git_operation_failed", "trusted repository comparison failed")
                    if ancestor.returncode != 0:
                        raise TeamError(
                            409,
                            "adoption_base_conflict",
                            "draft base is not an ancestor of the target workspace base",
                        )
                    merge = self._git_run(
                        [
                            "merge-tree",
                            "--write-tree",
                            "--merge-base",
                            source_base,
                            target_snapshot,
                            str(draft["draft_commit"]),
                        ],
                        repository,
                    )
                    if merge.returncode not in (0, 1):
                        raise TeamError(500, "git_operation_failed", "trusted repository merge failed")
                    if merge.returncode != 0:
                        result: dict[str, object] = {
                            "status": "conflict",
                            "adoptionId": adoption_id,
                            "spaceId": normalized_space,
                            "workspaceId": str(target_workspace["workspace_id"]),
                            "targetSessionId": target_session,
                            "draftId": normalized_draft,
                            "sourceDraftCommit": str(draft["draft_commit"]),
                            "targetTreeCommit": target_snapshot,
                            "message": "draft conflicts with target workspace; target was left unchanged",
                        }
                        return self._record_adoption(
                            adoption_id=adoption_id,
                            space_id=normalized_space,
                            workspace_id=str(target_workspace["workspace_id"]),
                            session_id=target_session,
                            draft_id=normalized_draft,
                            draft_commit=str(draft["draft_commit"]),
                            base_commit=source_base,
                            target_tree_commit=target_snapshot,
                            status="conflict",
                            result=result,
                        )
                    output = merge.stdout
                    text_output = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
                    first_line = text_output.strip().splitlines()[0] if text_output.strip() else ""
                    tree = first_line.split()[0] if first_line else ""
                    if not re.fullmatch(r"[0-9a-f]{40,64}", tree):
                        raise TeamError(500, "git_operation_failed", "trusted repository merge was invalid")
                    adoption_commit = self._git_commit_tree(
                        repository,
                        tree,
                        (target_snapshot, str(draft["draft_commit"])),
                        f"Adopt draft {normalized_draft} into {target_session}",
                    )
                    with self._db(write=True) as write_conn:
                        # Recheck every authority immediately before changing
                        # the Agent-owned directory.  The write transaction
                        # fences concurrent disable/removal operations.
                        self._require_space_conn(write_conn, actor, normalized_space, "write")
                        self._current_binding_conn(write_conn, target_session)
                        current_draft = write_conn.execute(
                            "SELECT * FROM team_drafts WHERE id = ? AND space_id = ?",
                            (normalized_draft, normalized_space),
                        ).fetchone()
                        if current_draft is None:
                            raise _not_found()
                        current_workspace = write_conn.execute(
                            "SELECT * FROM team_workspaces WHERE space_id = ? AND workspace_id = ?",
                            (normalized_space, str(target_workspace["workspace_id"])),
                        ).fetchone()
                        if current_workspace is None:
                            raise _denied()
                        current_path = self._workspace_path_is_expected(current_workspace)
                        if current_path != target_path or str(current_workspace["base_commit"]) != target_base:
                            raise TeamError(409, "workspace_changed", "target workspace changed during adoption")
                        existing = write_conn.execute(
                            "SELECT result_json FROM team_workspace_adoptions WHERE workspace_id = ? AND draft_id = ?",
                            (str(current_workspace["workspace_id"]), normalized_draft),
                        ).fetchone()
                        if existing is not None:
                            return self._adoption_result(existing["result_json"])
                        try:
                            self._clear_workspace_worktree(target_path)
                            self._materialize_commit(repository, adoption_commit, target_path)
                        except Exception:
                            # Best-effort restoration from the immutable
                            # pre-adoption snapshot keeps a failed write from
                            # leaving a half-cleared Agent workspace.
                            try:
                                self._clear_workspace_worktree(target_path)
                                self._materialize_commit(repository, target_snapshot, target_path)
                            except Exception:
                                pass
                            raise
                        result = {
                            "status": "adopted",
                            "adoptionId": adoption_id,
                            "spaceId": normalized_space,
                            "workspaceId": str(current_workspace["workspace_id"]),
                            "targetSessionId": target_session,
                            "draftId": normalized_draft,
                            "sourceDraftCommit": str(current_draft["draft_commit"]),
                            "targetTreeCommit": adoption_commit,
                            "message": "draft adopted into target workspace",
                        }
                        encoded = _safe_json(result)
                        now = self._now_ms()
                        write_conn.execute(
                            "UPDATE team_workspaces SET source_draft_id = ?, source_draft_commit = ?, source_adopted_at_ms = ? WHERE space_id = ? AND workspace_id = ?",
                            (normalized_draft, str(current_draft["draft_commit"]), now, normalized_space, str(current_workspace["workspace_id"])),
                        )
                        write_conn.execute(
                            "INSERT INTO team_workspace_adoptions(id,space_id,workspace_id,session_id,draft_id,draft_commit,base_commit,target_tree_commit,status,result_json,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (adoption_id, normalized_space, str(current_workspace["workspace_id"]), target_session, normalized_draft, str(current_draft["draft_commit"]), source_base, adoption_commit, "adopted", encoded, now),
                        )
                        return result
            finally:
                if snapshot_stage.exists() and not snapshot_stage.is_symlink():
                    shutil.rmtree(snapshot_stage)

    def _integration_authorize(
        self,
        conn: sqlite3.Connection,
        actor: str,
        space_id: str,
        *,
        automation: bool,
        preauthorized: bool,
    ) -> dict[str, object] | None:
        if automation:
            if not preauthorized or actor not in {"automation", "system"}:
                raise TeamError(403, "forbidden", "integration automation is not preauthorized")
            return None
        return self._require_space_conn(conn, actor, space_id, "manage")

    def _integration_context(
        self,
        draft_id: str,
        actor: str,
        target_branch: str | None,
        *,
        automation: bool,
        preauthorized: bool,
    ) -> dict[str, object]:
        with self._db() as conn:
            draft = conn.execute("SELECT * FROM team_drafts WHERE id = ?", (draft_id,)).fetchone()
            if draft is None:
                raise _not_found()
            space_id = str(draft["space_id"])
            branch = str(draft["target_branch"])
            if target_branch is not None and target_branch != branch:
                raise TeamError(409, "target_branch_conflict", "draft belongs to another target branch")
            scope = self._integration_authorize(
                conn,
                actor,
                space_id,
                automation=automation,
                preauthorized=preauthorized,
            )
            if scope is not None and scope["kind"] != "project":
                raise TeamError(403, "project_delivery_forbidden", "personal workspaces cannot be integrated")
            # A pending draft is a candidate for the current project brief.
            # Check this before doing merge work so a known stale result cannot
            # reach verification or the ref update.  Terminal receipts remain
            # readable and replayable below even after a newer brief exists.
            if str(draft["status"]) == "pending":
                draft_requirements_revision = self._workspace_requirements_revision(draft)
                current_requirements_revision = self._latest_requirements_revision_conn(
                    conn,
                    space_id,
                )
                if draft_requirements_revision != current_requirements_revision:
                    raise TeamError(
                        409,
                        "stale_requirements",
                        "draft requirements are stale; publish a new draft from the current project brief",
                    )
            # Sharing already checked the source Session grant.  A published
            # draft is a project artifact, so its owner may later leave the
            # project without revoking a manager's ability to deliver it.
            project = self._project_row(conn, space_id, branch)
            repository = self._assert_repo_path(Path(str(project["repository_path"])))
            actual_head = self._repo_revision(repository, branch)
            intent = conn.execute(
                "SELECT * FROM team_integration_intents WHERE draft_id = ? AND expected_head = ?",
                (draft_id, actual_head),
            ).fetchone()
            latest_intent = conn.execute(
                "SELECT * FROM team_integration_intents WHERE draft_id = ? ORDER BY created_at_ms DESC, id DESC LIMIT 1",
                (draft_id,),
            ).fetchone()
            return {
                "draft": draft,
                "spaceId": space_id,
                "branch": branch,
                "project": project,
                "repository": repository,
                "actualHead": actual_head,
                "intent": intent or latest_intent,
            }

    def _ensure_intent(
        self,
        draft_id: str,
        space_id: str,
        branch: str,
        expected_head: str,
        candidate_commit: str,
    ) -> sqlite3.Row:
        with self._db(write=True) as conn:
            existing = conn.execute(
                "SELECT * FROM team_integration_intents WHERE draft_id = ? AND expected_head = ?",
                (draft_id, expected_head),
            ).fetchone()
            if existing is not None:
                if str(existing["candidate_commit"]) != candidate_commit:
                    raise TeamError(409, "integration_conflict", "integration intent is bound to another candidate")
                return cast(sqlite3.Row, existing)
            intent_id = _new_id("intent")
            now = self._now_ms()
            conn.execute(
                "INSERT INTO team_integration_intents(id,draft_id,space_id,target_branch,expected_head,candidate_commit,status,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?, ?, ?)",
                (intent_id, draft_id, space_id, branch, expected_head, candidate_commit, "prepared", now, now),
            )
            row = conn.execute("SELECT * FROM team_integration_intents WHERE id = ?", (intent_id,)).fetchone()
            assert row is not None
            return cast(sqlite3.Row, row)

    def _terminal_without_ref(
        self,
        intent_id: str,
        draft_id: str,
        status: str,
        result: Mapping[str, object],
        verifier_result: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        intent_status = "verifier_failed" if status == "verification_failed" else status
        with self._db(write=True) as conn:
            draft = conn.execute("SELECT * FROM team_drafts WHERE id = ?", (draft_id,)).fetchone()
            if draft is None:
                raise _not_found()
            if str(draft["status"]) != "pending" and draft["result_json"]:
                return _load_json_object(draft["result_json"])
            enriched_result = self._with_draft_delivery_metadata(draft, result)
            encoded_result = _safe_json(enriched_result)
            now = self._now_ms()
            conn.execute(
                "UPDATE team_integration_intents SET status = ?, verifier_json = ?, result_json = ?, updated_at_ms = ? WHERE id = ?",
                (
                    intent_status,
                    _safe_json(dict(verifier_result)) if verifier_result is not None else None,
                    encoded_result,
                    now,
                    intent_id,
                ),
            )
            conn.execute(
                "UPDATE team_drafts SET status = ?, result_json = ?, integrated_at_ms = CASE WHEN ? = 'integrated' THEN ? ELSE integrated_at_ms END, integrated_commit = CASE WHEN ? = 'integrated' THEN ? ELSE integrated_commit END WHERE id = ?",
                (status, encoded_result, status, now, status, enriched_result.get("headCommit"), draft_id),
            )
        return enriched_result

    def _cas_and_finalize(
        self,
        intent: sqlite3.Row,
        draft_id: str,
        actor: str,
        candidate_commit: str,
        expected_head: str,
        *,
        automation: bool,
        preauthorized: bool,
    ) -> dict[str, object]:
        with self._db(write=True) as conn:
            draft = conn.execute("SELECT * FROM team_drafts WHERE id = ?", (draft_id,)).fetchone()
            if draft is None:
                raise _not_found()
            if str(draft["status"]) != "pending" and draft["result_json"]:
                return _load_json_object(draft["result_json"])
            space_id = str(draft["space_id"])
            branch = str(draft["target_branch"])
            self._integration_authorize(
                conn,
                actor,
                space_id,
                automation=automation,
                preauthorized=preauthorized,
            )
            draft_requirements_revision = self._workspace_requirements_revision(draft)
            current_requirements_revision = self._latest_requirements_revision_conn(conn, space_id)
            if draft_requirements_revision != current_requirements_revision:
                raise TeamError(
                    409,
                    "stale_requirements",
                    "draft requirements are stale; publish a new draft from the current project brief",
                )
            project = self._project_row(conn, space_id, branch)
            repository = self._assert_repo_path(Path(str(project["repository_path"])))
            actual_head = self._repo_revision(repository, branch)
            now = self._now_ms()
            if actual_head == candidate_commit:
                self._replace_repository_worktree(repository, candidate_commit)
                result: dict[str, object] = {
                    "status": "integrated",
                    "draftId": draft_id,
                    "spaceId": space_id,
                    "targetBranch": branch,
                    "expectedHead": expected_head,
                    "candidateCommit": candidate_commit,
                    "headCommit": candidate_commit,
                    "message": "draft integrated",
                }
                result = self._with_draft_delivery_metadata(draft, result)
                conn.execute(
                    "UPDATE team_integration_intents SET status = 'applied', result_json = ?, updated_at_ms = ? WHERE id = ?",
                    (_safe_json(result), now, str(intent["id"])),
                )
                conn.execute(
                    "UPDATE team_drafts SET status = 'integrated', result_json = ?, integrated_at_ms = ?, integrated_commit = ? WHERE id = ? AND status = 'pending'",
                    (_safe_json(result), now, candidate_commit, draft_id),
                )
                if str(project["head_commit"]) != candidate_commit:
                    conn.execute(
                        "UPDATE team_projects SET head_commit = ?, revision = revision + 1, updated_at_ms = ? WHERE space_id = ? AND target_branch = ?",
                        (candidate_commit, now, space_id, branch),
                    )
                return result
            if actual_head != expected_head:
                result = {
                    "status": "conflict",
                    "draftId": draft_id,
                    "spaceId": space_id,
                    "targetBranch": branch,
                    "expectedHead": expected_head,
                    "headCommit": actual_head,
                    "candidateCommit": candidate_commit,
                    "message": "target changed while the draft was being verified; refresh and publish a new draft",
                }
                result = self._with_draft_delivery_metadata(draft, result)
                conn.execute(
                    "UPDATE team_integration_intents SET status = 'conflict', result_json = ?, updated_at_ms = ? WHERE id = ?",
                    (_safe_json(result), now, str(intent["id"])),
                )
                conn.execute(
                    "UPDATE team_drafts SET status = 'conflict', result_json = ? WHERE id = ? AND status = 'pending'",
                    (_safe_json(result), draft_id),
                )
                if str(project["head_commit"]) != actual_head:
                    conn.execute(
                        "UPDATE team_projects SET head_commit = ?, revision = revision + 1, updated_at_ms = ? WHERE space_id = ? AND target_branch = ?",
                        (actual_head, now, space_id, branch),
                    )
                return result
            completed = self._git_run(
                ["update-ref", f"refs/heads/{branch}", candidate_commit, expected_head],
                repository,
            )
            if completed.returncode != 0:
                observed = self._repo_revision(repository, branch)
                result = {
                    "status": "conflict",
                    "draftId": draft_id,
                    "spaceId": space_id,
                    "targetBranch": branch,
                    "expectedHead": expected_head,
                    "headCommit": observed,
                    "candidateCommit": candidate_commit,
                    "message": "target changed before the atomic update; refresh and publish a new draft",
                }
                result = self._with_draft_delivery_metadata(draft, result)
                conn.execute(
                    "UPDATE team_integration_intents SET status = 'conflict', result_json = ?, updated_at_ms = ? WHERE id = ?",
                    (_safe_json(result), now, str(intent["id"])),
                )
                conn.execute(
                    "UPDATE team_drafts SET status = 'conflict', result_json = ? WHERE id = ? AND status = 'pending'",
                    (_safe_json(result), draft_id),
                )
                if str(project["head_commit"]) != observed:
                    conn.execute(
                        "UPDATE team_projects SET head_commit = ?, revision = revision + 1, updated_at_ms = ? WHERE space_id = ? AND target_branch = ?",
                        (observed, now, space_id, branch),
                    )
                return result
            self._replace_repository_worktree(repository, candidate_commit)
            result = {
                "status": "integrated",
                "draftId": draft_id,
                "spaceId": space_id,
                "targetBranch": branch,
                "expectedHead": expected_head,
                "candidateCommit": candidate_commit,
                "headCommit": candidate_commit,
                "message": "draft integrated",
            }
            result = self._with_draft_delivery_metadata(draft, result)
            conn.execute(
                "UPDATE team_integration_intents SET status = 'applied', result_json = ?, updated_at_ms = ? WHERE id = ?",
                (_safe_json(result), now, str(intent["id"])),
            )
            conn.execute(
                "UPDATE team_drafts SET status = 'integrated', result_json = ?, integrated_at_ms = ?, integrated_commit = ? WHERE id = ? AND status = 'pending'",
                (_safe_json(result), now, candidate_commit, draft_id),
            )
            conn.execute(
                "UPDATE team_projects SET head_commit = ?, revision = revision + 1, updated_at_ms = ? WHERE space_id = ? AND target_branch = ?",
                (candidate_commit, now, space_id, branch),
            )
            return result

    def integrate_draft(
        self,
        draft_id: object,
        actor_user_id: object,
        *,
        target_branch: object | None = None,
        automation: bool = False,
        preauthorized: bool = False,
    ) -> dict[str, object]:
        """Merge a fixed draft onto the latest target using a CAS ref update."""

        normalized_draft = _identifier(draft_id, "draft_id")
        actor = _identifier(actor_user_id, "actor_user_id")
        branch = _branch(target_branch) if target_branch is not None else None
        if not isinstance(automation, bool) or not isinstance(preauthorized, bool):
            raise _invalid("automation", "must be boolean")
        with self._lock:
            context = self._integration_context(
                normalized_draft,
                actor,
                branch,
                automation=automation,
                preauthorized=preauthorized,
            )
            draft = cast(sqlite3.Row, context["draft"])
            if str(draft["status"]) != "pending" and draft["result_json"]:
                return _load_json_object(draft["result_json"])
            space_id = str(context["spaceId"])
            target = str(context["branch"])
            repository = cast(Path, context["repository"])
            expected_head = str(context["actualHead"])
            existing_intent = cast(sqlite3.Row | None, context["intent"])
            if existing_intent is not None and str(existing_intent["status"]) in {
                "verifier_failed",
                "conflict",
                "applied",
            }:
                if existing_intent["result_json"]:
                    return _load_json_object(existing_intent["result_json"])
            if existing_intent is not None and str(existing_intent["status"]) == "prepared":
                candidate_commit = str(existing_intent["candidate_commit"])
                # A process can die after the CAS ref update and before the
                # final SQLite receipt.  The durable intent plus the observed
                # candidate ref is sufficient to complete that receipt.
                if candidate_commit and expected_head == candidate_commit:
                    return self._cas_and_finalize(
                        existing_intent,
                        normalized_draft,
                        actor,
                        candidate_commit,
                        str(existing_intent["expected_head"]),
                        automation=automation,
                        preauthorized=preauthorized,
                    )
                if str(existing_intent["expected_head"]) != expected_head:
                    result: dict[str, object] = {
                        "status": "conflict",
                        "draftId": normalized_draft,
                        "spaceId": space_id,
                        "targetBranch": target,
                        "expectedHead": str(existing_intent["expected_head"]),
                        "headCommit": expected_head,
                        "candidateCommit": candidate_commit,
                        "message": "target changed while an earlier integration was pending; publish a new draft",
                    }
                    return self._terminal_without_ref(
                        str(existing_intent["id"]),
                        normalized_draft,
                        "conflict",
                        result,
                    )
                intent = existing_intent
            else:
                merge = self._git_run(
                    ["merge-tree", "--write-tree", expected_head, str(draft["draft_commit"])],
                    repository,
                )
                if merge.returncode not in (0, 1):
                    raise TeamError(500, "git_operation_failed", "trusted repository merge failed")
                if merge.returncode != 0:
                    intent = self._ensure_intent(
                        normalized_draft,
                        space_id,
                        target,
                        expected_head,
                        "",
                    )
                    result = {
                        "status": "conflict",
                        "draftId": normalized_draft,
                        "spaceId": space_id,
                        "targetBranch": target,
                        "expectedHead": expected_head,
                        "headCommit": expected_head,
                        "candidateCommit": None,
                        "message": "draft conflicts with the latest target; resolve the conflict and publish a new draft",
                    }
                    return self._terminal_without_ref(
                        str(intent["id"]),
                        normalized_draft,
                        "conflict",
                        result,
                    )
                output = merge.stdout
                text_output = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
                first_line = text_output.strip().splitlines()[0] if text_output.strip() else ""
                tree = first_line.split()[0] if first_line else ""
                if not re.fullmatch(r"[0-9a-f]{40,64}", tree):
                    raise TeamError(500, "git_operation_failed", "trusted repository merge was invalid")
                candidate_commit = self._git_commit_tree(
                    repository,
                    tree,
                    (expected_head, str(draft["draft_commit"])),
                    f"Integrate draft {normalized_draft}",
                )
                intent = self._ensure_intent(
                    normalized_draft,
                    space_id,
                    target,
                    expected_head,
                    candidate_commit,
                )

            if not candidate_commit:
                raise TeamError(500, "integration_metadata_invalid", "integration intent is invalid")
            if self.verifier is None:
                raise TeamError(503, "verifier_unavailable", "integration verifier is not configured")
            candidate_stage = self.staging_root / f"candidate-{str(intent['id'])}"
            candidate_source = candidate_stage / "source"
            created_candidate_stage = False
            verifier_result: dict[str, object]
            try:
                if candidate_stage.exists() or candidate_stage.is_symlink():
                    raise TeamError(409, "integration_in_progress", "integration staging is busy")
                candidate_stage.mkdir(mode=0o700, parents=False)
                created_candidate_stage = True
                candidate_source.mkdir(mode=0o700, parents=False)
                self._materialize_commit(repository, candidate_commit, candidate_source)
                self._set_candidate_read_only(candidate_source)
                verifier_context = {
                    "candidatePath": str(candidate_source),
                    "temporaryPath": str(candidate_stage),
                    "actorUserId": actor,
                    "spaceId": space_id,
                    "draftId": normalized_draft,
                    "targetBranch": target,
                    "candidateCommit": candidate_commit,
                }
                try:
                    raw_verifier = _call_compatible(
                        self.verifier,
                        ((candidate_source, verifier_context), (candidate_source,), (verifier_context,)),
                    )
                    verified, verifier_result = _safe_verifier_result(raw_verifier)
                except Exception:
                    verified = False
                    verifier_result = {"ok": False, "error": "verifier_failed"}
            finally:
                if created_candidate_stage:
                    self._remove_candidate_stage(candidate_stage)
            if not verified:
                result = {
                    "status": "verification_failed",
                    "draftId": normalized_draft,
                    "spaceId": space_id,
                    "targetBranch": target,
                    "expectedHead": expected_head,
                    "headCommit": expected_head,
                    "candidateCommit": candidate_commit,
                    "verifier": verifier_result,
                    "message": "candidate verification failed; target was left unchanged",
                }
                return self._terminal_without_ref(
                    str(intent["id"]),
                    normalized_draft,
                    "verification_failed",
                    result,
                    verifier_result,
                )
            # Keep successful verification bound to this exact candidate before
            # touching Git. A later preview may reuse this receipt; a prepared
            # intent without a receipt never establishes successful validation.
            with self._db(write=True) as conn:
                conn.execute(
                    "UPDATE team_integration_intents SET verifier_json = ?, updated_at_ms = ? WHERE id = ? AND status = 'prepared'",
                    (_safe_json(verifier_result), self._now_ms(), str(intent["id"])),
                )
            return self._cas_and_finalize(
                intent,
                normalized_draft,
                actor,
                candidate_commit,
                expected_head,
                automation=automation,
                preauthorized=preauthorized,
            )

    def preview_baseline(self, actor: str, space_id: str) -> dict[str, object]:
        """Read the primary stable branch without provisioning a repository."""
        scope = self.identity.require_space(actor, space_id, action="manage")
        if scope["kind"] != "project":
            raise _space_not_found()
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM team_projects WHERE space_id = ? ORDER BY CASE WHEN target_branch = 'main' THEN 0 ELSE 1 END, target_branch LIMIT 1",
                (space_id,),
            ).fetchone()
            if row is None:
                raise TeamError(409, "project_not_provisioned", "Provision a project repository before starting its preview")
            repository = self._assert_repo_path(Path(str(row["repository_path"])))
            if self._repo_revision(repository, str(row["target_branch"])) != str(row["head_commit"]):
                raise TeamError(409, "project_recovery_required", "Recover the stable repository before starting its preview")
            return {
                "branch": str(row["target_branch"]), "commit": str(row["head_commit"]),
                "requirementsRevision": self._latest_requirements_revision_conn(conn, space_id),
            }

    def _preview_stage(self, deployment_id: str) -> Path:
        if not re.fullmatch(r"pv-[0-9a-f]{32}", deployment_id):
            raise _invalid("deployment_id")
        return self.staging_root / ("preview-" + deployment_id)

    def remove_preview_source(self, deployment_id: str) -> None:
        """Called only after the controller proves the deployment is removed."""
        stage = self._preview_stage(deployment_id)
        with self._lock:
            if stage.exists() or stage.is_symlink():
                self._remove_candidate_stage(stage)

    def create_preview_source(
        self, actor: str, space_id: str, *, branch: str, commit: str,
        requirements_revision: int, deployment_id: str,
    ) -> Path:
        """Materialize and validate a frozen stable revision for an OCI preview.

        An applied integration receipt covers exactly its commit and brief.
        Imported repositories and older integrations without that receipt run
        the same configured isolated verifier before they can be previewed.
        """
        stage = self._preview_stage(deployment_id)
        source = stage / "source"
        scope = self.identity.require_space(actor, space_id, action="manage")
        if scope["kind"] != "project":
            raise _space_not_found()
        with self._lock, self._db() as conn:
            project = self._project_row(conn, space_id, _branch(branch))
            repository = self._assert_repo_path(Path(str(project["repository_path"])))
            if (str(project["head_commit"]) != _object_id(commit, "commit")
                    or self._repo_revision(repository, branch) != commit
                    or self._latest_requirements_revision_conn(conn, space_id) != requirements_revision):
                raise TeamError(409, "preview_baseline_changed", "Project changed before preview preparation; start the current version")
            receipt = conn.execute(
                "SELECT i.verifier_json FROM team_integration_intents i JOIN team_drafts d ON d.id = i.draft_id "
                "WHERE i.space_id = ? AND i.target_branch = ? AND i.candidate_commit = ? AND i.status = 'applied' "
                "AND d.requirements_revision = ? AND i.verifier_json IS NOT NULL ORDER BY i.updated_at_ms DESC LIMIT 1",
                (space_id, branch, commit, requirements_revision),
            ).fetchone()
            verified = receipt is not None and _safe_verifier_result(_load_json_object(receipt["verifier_json"]))[0]
            if stage.exists() or stage.is_symlink():
                raise TeamError(409, "preview_staging_busy", "Previous preview preparation needs cleanup")
            stage.mkdir(mode=0o700)
            try:
                source.mkdir(mode=0o700)
                self._materialize_commit(repository, commit, source)
                self._set_candidate_read_only(source)
            except BaseException:
                self._remove_candidate_stage(stage)
                raise
        try:
            if not verified:
                if self.verifier is None:
                    raise TeamError(503, "verifier_unavailable", "Configure isolated validation before starting a preview")
                context = {
                    "candidatePath": str(source), "temporaryPath": str(stage), "actorUserId": actor,
                    "spaceId": space_id, "targetBranch": branch, "candidateCommit": commit,
                    "requirementsRevision": requirements_revision,
                }
                raw = _call_compatible(self.verifier, ((source, context), (source,), (context,)))
                if not _safe_verifier_result(raw)[0]:
                    raise TeamError(409, "preview_validation_failed", "Project validation failed; the previous preview remains available")
            current = self.identity.require_space(actor, space_id, action="manage")
            if current["membershipRevision"] != scope["membershipRevision"]:
                raise TeamError(403, "preview_authorization_changed", "Project authorization changed during validation")
            return source
        except BaseException:
            self.remove_preview_source(deployment_id)
            raise


__all__ = ["TeamWorkspaceService"]
