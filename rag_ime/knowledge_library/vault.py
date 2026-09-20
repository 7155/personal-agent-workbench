"""Read-through Markdown connector. User files are authoritative, never copied.

Identity and authorization live in the existing Knowledge SQLite database.
This connector has no Provider, Agent loop, Memory adoption or old-note writer.
"""

from __future__ import annotations

import hashlib
import base64
import posixpath
import mimetypes
import json
import os
import re
import stat
import threading
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from .models import (
    KnowledgeConflictError,
    KnowledgeLibraryError,
    KnowledgeNotFoundError,
)
from .store import KnowledgeStore, now_ms

DEFAULT_EXCLUDES = [".obsidian", ".git", "日记", "私人", "private", "PAW生成"]
MAX_BYTES = 4 * 1024 * 1024
MAX_NOTES = 10_000
SCHEMA = "rag-ime.markdown-vault.v1"


def normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def relative(value: str) -> str:
    p = PurePosixPath(value)
    if (
        not value
        or p.is_absolute()
        or any(x in {".", "..", ""} for x in value.split("/"))
        or "\\" in value
        or "\x00" in value
    ):
        raise KnowledgeLibraryError("需要文件夹内的相对路径。", code="invalid_argument")
    return p.as_posix()


class MarkdownVault:
    def __init__(self, store: KnowledgeStore):
        self.store = store
        self.lock = threading.RLock()
        self._metadata_cache = {}
        self.activity_provider = None
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge_vaults (
                    id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    excluded_json TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
                    created_at_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS knowledge_vault_notes (
                    id TEXT PRIMARY KEY, vault_id TEXT NOT NULL REFERENCES knowledge_vaults(id),
                    path TEXT NOT NULL, revision TEXT NOT NULL, device INTEGER NOT NULL,
                    inode INTEGER NOT NULL, explicit_id TEXT NOT NULL DEFAULT '',
                    identity_state TEXT NOT NULL DEFAULT 'tracked', present INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX IF NOT EXISTS knowledge_vault_note_paths ON knowledge_vault_notes(vault_id,path);
            """)

        from .vault_workflow import VaultWorkflow

        self.workflow = VaultWorkflow(self)

    def dispatch(self, payload: dict) -> dict:
        with self.lock:
            action = payload.get("action", "list")
            if action == "list":
                with self.store.connection() as db:
                    spaces = [
                        self._space(row)
                        for row in db.execute(
                            "SELECT * FROM knowledge_vaults ORDER BY created_at_ms"
                        )
                    ]
                return {"schemaVersion": SCHEMA, "spaces": spaces}
            if action == "connect":
                return self.connect(payload)
            vault = self._vault(str(payload.get("vaultId", "")))
            if action == "pause":
                if not isinstance(payload.get("paused"), bool):
                    raise KnowledgeLibraryError(
                        "需要明确暂停状态。", code="invalid_argument"
                    )
                with self.store.connection() as db:
                    db.execute(
                        "UPDATE knowledge_vaults SET paused=? WHERE id=?",
                        (int(payload["paused"]), vault["id"]),
                    )
                self._metadata_cache.clear()
                return {"schemaVersion": SCHEMA, "paused": payload["paused"]}
            if vault["paused"]:
                raise KnowledgeLibraryError("此文件夹已暂停读取。", code="vault_paused")
            if action == "read":
                return self.read(vault, str(payload.get("noteId", "")))
            if action in {"resolve", "attachment"}:
                return self.link(
                    vault,
                    str(payload.get("noteId", "")),
                    str(payload.get("link", "")),
                    binary=action == "attachment",
                )
            if action == "snapshot":
                return self.snapshot(
                    vault,
                    str(payload.get("query", "")),
                    str(payload.get("focusId", "")),
                )
            return self.workflow.dispatch(vault, payload)

    def connect(self, payload: dict) -> dict:
        root = Path(str(payload.get("root", ""))).expanduser()
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise KnowledgeLibraryError(
                "请选择现有的本地笔记文件夹。", code="invalid_argument"
            )
        root = root.resolve(strict=True)
        excluded = payload.get("excluded", DEFAULT_EXCLUDES)
        if not isinstance(excluded, list) or any(
            not isinstance(p, str) for p in excluded
        ):
            raise KnowledgeLibraryError(
                "排除目录必须是相对路径列表。", code="invalid_argument"
            )
        excluded = sorted(set(relative(p.strip()) for p in excluded if p.strip()))
        # The control database must never be created inside a user Vault.
        if self.store.database_path.resolve().is_relative_to(root):
            raise KnowledgeLibraryError(
                "请选择笔记目录，不要选择 PAW 运行数据的父目录。",
                code="invalid_argument",
            )
        with self.store.connection() as db:
            current = db.execute(
                "SELECT * FROM knowledge_vaults WHERE root=?", (str(root),)
            ).fetchone()
            if current:
                # Reconnecting does not silently change prior exclusions.
                return {"schemaVersion": SCHEMA, "space": self._space(current)}
            identity = str(uuid.uuid4())
            db.execute(
                "INSERT INTO knowledge_vaults VALUES(?,?,?,?,0,?)",
                (
                    identity,
                    str(root),
                    root.name,
                    json.dumps(excluded, ensure_ascii=False),
                    now_ms(),
                ),
            )
        return {"schemaVersion": SCHEMA, "space": self._space(self._vault(identity))}

    def _vault(self, identity: str):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM knowledge_vaults WHERE id=?", (identity,)
            ).fetchone()
        if not row:
            raise KnowledgeNotFoundError("笔记文件夹不存在。")
        result = dict(row)
        with self.store.connection() as db:
            policy = db.execute(
                "SELECT policy_json FROM knowledge_vault_policy WHERE vault_id=?",
                (identity,),
            ).fetchone()
        result["personal_pattern"] = (
            json.loads(policy[0]).get("personalDiary", "") if policy else ""
        )
        return result

    @staticmethod
    def _space(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "root": row["root"],
            "excluded": json.loads(row["excluded_json"]),
            "paused": bool(row["paused"]),
            "remoteProcessing": False,
            "existingNoteWrite": "editor_only",
        }

    @staticmethod
    def _allowed(vault, path: str) -> bool:
        parts = PurePosixPath(path).parts
        pattern = vault.get("personal_pattern", "")
        if pattern and re.fullmatch(
            re.escape(pattern).replace(r"\{date\}", r"\d{4}-\d{2}-\d{2}"), path
        ):
            return False
        return not any(p.startswith(".") for p in parts) and not any(
            path == p or path.startswith(p + "/")
            for p in json.loads(vault["excluded_json"])
        )

    def _read_path(self, vault, path: str, *, binary=False):
        path = relative(path)
        if not self._allowed(vault, path) or PurePosixPath(path).suffix.lower() not in (
            {".png", ".jpg", ".jpeg", ".webp", ".gif"}
            if binary
            else {".md", ".markdown"}
        ):
            raise KnowledgeNotFoundError("笔记不在可读范围。")
        # Open every directory component without following symlinks, including
        # the configured root. Replacing a parent with a symlink cannot escape.
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in Path(vault["root"]).parts[1:]:
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        try:
            parts = PurePosixPath(path).parts
            for part in parts[:-1]:
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = next_fd
            source = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            with os.fdopen(source, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
                    raise KnowledgeLibraryError(
                        "笔记超过 4 MiB 或不是普通文件。", code="source_too_large"
                    )
                body = stream.read(MAX_BYTES + 1)
                after = os.fstat(stream.fileno())
                if len(body) > MAX_BYTES or (before.st_mtime_ns, before.st_size) != (
                    after.st_mtime_ns,
                    after.st_size,
                ):
                    raise KnowledgeConflictError("笔记正在变化，请重新读取。")
                return (body if binary else body.decode("utf-8-sig")), after
        finally:
            os.close(fd)

    @staticmethod
    def _metadata(path: str, body: str) -> dict:
        # Recognize simple, portable metadata. Never evaluate YAML tags.
        front = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", body, re.S)
        metadata = front.group(1) if front else ""

        def field(key):
            match = re.search(rf"(?m)^{key}:\s*([^\n]+)$", metadata)
            return match.group(1).strip().strip("\"'") if match else ""

        aliases = field("aliases").strip("[]")
        alias_list = [s.strip().strip("\"'") for s in aliases.split(",") if s.strip()]
        match = re.search(r"(?m)^aliases:\s*\n((?:[ \t]+-[^\n]*\n?)+)", metadata)
        if match:
            alias_list += [
                s.strip().strip("\"'")
                for s in re.findall(r"(?m)^\s*-\s*(.+)$", match.group(1))
            ]
        heading = re.search(r"(?m)^#\s+(.+)$", body)
        links = [
            (m.group(1).split("|")[0].strip())
            for m in re.finditer(r"(?<!!)\[\[([^\]\n]+)\]\]", body)
        ]
        links += [
            unquote(m.group(1).strip("<>"))
            for m in re.finditer(r"(?<!!)\[[^\]\n]*\]\(([^\s)]+)\)", body)
            if "://" not in m.group(1)
        ]
        return {
            "title": field("title")
            or (heading.group(1).strip() if heading else PurePosixPath(path).stem),
            "aliases": alias_list,
            "explicitId": field("paw_note_id"),
            "links": links,
        }

    def snapshot(self, vault, query: str = "", focus_id: str = "") -> dict:
        discovered = []
        omitted = 0
        total_bytes = 0
        root = Path(vault["root"])
        if root.is_symlink() or not root.is_dir() or root.resolve() != root:
            raise KnowledgeLibraryError(
                "文件夹身份已改变，请重新连接。", code="scope_mismatch"
            )
        for directory, dirs, files in os.walk(root, followlinks=False):
            rel_dir = Path(directory).relative_to(root)
            dirs[:] = sorted(
                d
                for d in dirs
                if not (Path(directory) / d).is_symlink()
                and self._allowed(vault, (rel_dir / d).as_posix())
            )
            for filename in sorted(files):
                path = (rel_dir / filename).as_posix()
                if not self._allowed(vault, path) or Path(path).suffix.lower() not in {
                    ".md",
                    ".markdown",
                }:
                    continue
                if len(discovered) >= MAX_NOTES or total_bytes >= 32 * 1024 * 1024:
                    omitted += 1
                    continue
                if (root / path).is_symlink():
                    continue
                try:
                    info = (root / path).stat(follow_symlinks=False)
                    fingerprint = (
                        info.st_dev,
                        info.st_ino,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                    )
                    cached = self._metadata_cache.get((vault["id"], path))
                    if cached and cached[0] == fingerprint:
                        item = dict(cached[1])
                    else:
                        body, info = self._read_path(vault, path)
                        item = {
                            "path": path,
                            "body": body,
                            "revision": hashlib.sha256(body.encode()).hexdigest(),
                            "device": info.st_dev,
                            "inode": info.st_ino,
                            **self._metadata(path, body),
                        }
                        self._metadata_cache[(vault["id"], path)] = (
                            fingerprint,
                            dict(item),
                        )
                    total_bytes += info.st_size
                    discovered.append(item)
                except (OSError, UnicodeError, KnowledgeLibraryError):
                    omitted += 1
        current_cache_keys = {(vault["id"], item["path"]) for item in discovered}
        self._metadata_cache = {
            k: value
            for k, value in self._metadata_cache.items()
            if k[0] != vault["id"] or k in current_cache_keys
        }
        with self.store.connection() as db:
            prior = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM knowledge_vault_notes WHERE vault_id=?",
                    (vault["id"],),
                )
            ]
            current_paths = {item["path"] for item in discovered}
            explicit_counts = {}
            for item in discovered:
                if item["explicitId"]:
                    explicit_counts[item["explicitId"]] = (
                        explicit_counts.get(item["explicitId"], 0) + 1
                    )
            used = set()
            for item in discovered:
                same_path = [
                    p for p in prior if p["path"] == item["path"] and p["present"]
                ]
                matches = same_path
                if (
                    not matches
                    and item["explicitId"]
                    and explicit_counts[item["explicitId"]] == 1
                ):
                    matches = [
                        p
                        for p in prior
                        if p["explicit_id"] == item["explicitId"]
                        and p["path"] not in current_paths
                    ]
                if not matches:
                    # An unchanged unique hash supports rename. Rename+edit
                    # without explicit identity is intentionally unresolved.
                    matches = [
                        p
                        for p in prior
                        if p["path"] not in current_paths
                        and p["revision"] == item["revision"]
                    ]
                    if sum(d["revision"] == item["revision"] for d in discovered) != 1:
                        matches = []
                chosen = (
                    matches[0]
                    if len(matches) == 1 and matches[0]["id"] not in used
                    else None
                )
                item["id"] = chosen["id"] if chosen else str(uuid.uuid4())
                item["identityState"] = (
                    "duplicate_id"
                    if item["explicitId"] and explicit_counts[item["explicitId"]] > 1
                    else "tracked"
                    if chosen
                    else "new_unlinked"
                )
                used.add(item["id"])
                db.execute(
                    "INSERT OR REPLACE INTO knowledge_vault_notes VALUES(?,?,?,?,?,?,?,?,1)",
                    (
                        item["id"],
                        vault["id"],
                        item["path"],
                        item["revision"],
                        item["device"],
                        item["inode"],
                        item["explicitId"],
                        item["identityState"],
                    ),
                )
            for row in prior:
                if row["id"] not in used:
                    db.execute(
                        "UPDATE knowledge_vault_notes SET present=0 WHERE id=?",
                        (row["id"],),
                    )
        self.workflow.capture_files(vault, discovered)
        # Resolve each link via an index rather than scanning every note.
        lookup = {}
        for n in discovered:
            keys = {
                n["path"],
                n["path"].removesuffix(".md"),
                PurePosixPath(n["path"]).stem,
                n["title"],
                *n["aliases"],
            }
            for key in keys:
                lookup.setdefault(normalized(key), {})[n["id"]] = n
        edges = []
        for item in discovered:
            for link in item["links"]:
                target, _, fragment = link.partition("#")
                base_path = (PurePosixPath(item["path"]).parent / target).as_posix()
                candidates = list(
                    (
                        {item["id"]: item}
                        if not target
                        else {
                            **lookup.get(normalized(target.removesuffix(".md")), {}),
                            **lookup.get(normalized(base_path), {}),
                        }
                    ).values()
                )
                if len(candidates) == 1:
                    other = candidates[0]
                    valid_fragment = not fragment or (
                        bool(
                            re.search(
                                rf"(?m)^#+\s+{re.escape(fragment)}\s*$", other["body"]
                            )
                        )
                        if not fragment.startswith("^")
                        else bool(
                            re.search(
                                rf"{re.escape(fragment)}\s*$", other["body"], re.M
                            )
                        )
                    )
                    edges.append(
                        {
                            "source": item["id"],
                            "target": other["id"],
                            "kind": "explicit_link",
                            "label": "引用了",
                            "locator": fragment,
                            "locatorValid": valid_fragment,
                            "sourceRevision": item["revision"],
                        }
                    )
        # Applied revisions are business relations, separate from text links.
        by_id = {n["id"]: n for n in discovered}
        with self.store.connection() as db:
            applied = db.execute(
                "SELECT * FROM knowledge_note_proposals WHERE vault_id=? AND state IN ('saved','saved_index_pending')",
                (vault["id"],),
            ).fetchall()
        for proposal in applied:
            for source in json.loads(proposal["sources_json"]):
                if source.get("noteId") in by_id and proposal["note_id"] in by_id:
                    current = (
                        by_id[source["noteId"]]["revision"] == source["revision"]
                        and by_id[proposal["note_id"]]["revision"]
                        == proposal["after_revision"]
                    )
                    edges.append(
                        {
                            "source": source["noteId"],
                            "target": proposal["note_id"],
                            "kind": "revision_source",
                            "label": "用于修订" if current else "历史修订依据",
                            "locator": proposal["application_id"],
                            "locatorValid": current,
                            "sourceRevision": source["revision"],
                        }
                    )
        terms = normalized(query).split()
        visible = [
            n
            for n in discovered
            if all(
                t
                in normalized(
                    n["title"]
                    + " "
                    + n["path"]
                    + " "
                    + " ".join(n["aliases"])
                    + " "
                    + n["body"]
                )
                for t in terms
            )
        ]
        if focus_id:
            ids = {focus_id}
            for _ in range(2):
                ids |= {e["target"] for e in edges if e["source"] in ids} | {
                    e["source"] for e in edges if e["target"] in ids
                }
            visible = [n for n in visible if n["id"] in ids]
        nodes = [
            {
                k: n[k]
                for k in ["id", "path", "title", "aliases", "revision", "identityState"]
            }
            for n in visible[:200]
        ]
        ids = {n["id"] for n in nodes}
        return {
            "schemaVersion": SCHEMA,
            "space": self._space(vault),
            "notes": nodes,
            "edges": [e for e in edges if e["source"] in ids and e["target"] in ids],
            "total": len(visible),
            "truncated": len(visible) > 200 or omitted > 0,
            "unreadableCount": omitted,
            "remoteProcessing": False,
        }

    def read(self, vault, note_id: str) -> dict:
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM knowledge_vault_notes WHERE id=? AND vault_id=? AND present=1",
                (note_id, vault["id"]),
            ).fetchone()
        if not row:
            raise KnowledgeNotFoundError("笔记身份失效，请刷新目录。")
        try:
            body, info = self._read_path(vault, row["path"])
        except OSError as exc:
            raise KnowledgeNotFoundError("笔记已移动或不可读，请刷新目录。") from exc
        return {
            "schemaVersion": SCHEMA,
            "noteId": note_id,
            "path": row["path"],
            "markdown": body,
            "revision": hashlib.sha256(body.encode()).hexdigest(),
            "obsidianUri": "obsidian://open?path="
            + quote(str(Path(vault["root"]) / row["path"]), safe=""),
            "remoteProcessing": False,
        }

    def link(self, vault, note_id, link, *, binary=False):
        note = self.read(vault, note_id)
        target, _, fragment = unquote(link).partition("#")
        if "://" in target or target.startswith("/") or "\\" in target:
            raise KnowledgeNotFoundError("只支持笔记库内的相对引用。")
        path = (
            posixpath.normpath(posixpath.join(posixpath.dirname(note["path"]), target))
            if target
            else note["path"]
        )
        relative(path)
        if binary:
            data, _ = self._read_path(vault, path, binary=True)
            return {
                "dataUrl": "data:"
                + (mimetypes.guess_type(path)[0] or "image/png")
                + ";base64,"
                + base64.b64encode(data).decode()
            }
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id,path FROM knowledge_vault_notes WHERE vault_id=? AND present=1",
                (vault["id"],),
            ).fetchall()
        candidates = [r for r in rows if r["path"] == path or r["path"] == path + ".md"]
        if not candidates and "/" not in target:
            candidates = [
                r
                for r in rows
                if PurePosixPath(r["path"]).stem == target.removesuffix(".md")
            ]
        if len(candidates) != 1:
            raise KnowledgeNotFoundError("引用不存在或名称有歧义，请在目录中选择。")
        result = self.read(vault, candidates[0]["id"])
        result["locator"] = fragment
        result["locatorValid"] = (
            not fragment
            or bool(
                re.search(rf"(?m)^#+\s+{re.escape(fragment)}\s*$", result["markdown"])
            )
            or (
                fragment.startswith("^")
                and bool(
                    re.search(rf"{re.escape(fragment)}\s*$", result["markdown"], re.M)
                )
            )
        )
        return result
