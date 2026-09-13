from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

from .agent_media import AgentMediaStore, IMAGE_MIME_TYPES, TEXT_MEDIA_MIME_TYPES
from .contracts.json_schema import validate_contract


MAX_TEXT_PREVIEW_BYTES = 512 * 1024
_TEXT_PREVIEW_KINDS = frozenset({"markdown", "code", "diff", "html"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LANGUAGE_BY_EXTENSION = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "jsx",
    ".json": "json",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".m": "objective-c",
    ".md": "markdown",
    ".mdx": "markdown",
    ".php": "php",
    ".plist": "xml",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scss": "scss",
    ".sh": "shellscript",
    ".sql": "sql",
    ".svelte": "svelte",
    ".swift": "swift",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".txt": "text",
    ".vue": "vue",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".zsh": "shellscript",
}


class AgentFilePreviewReader:
    """Authorize one immutable media object and project a bounded UI preview."""

    def __init__(self, media: AgentMediaStore) -> None:
        self.media = media

    def read(
        self,
        media_id: str,
        *,
        session_id: str,
        expected_sha256: str = "",
        room_id: str = "",
    ) -> dict[str, object]:
        receipt, raw = self.media.read(media_id, session_id="" if room_id else session_id, room_id=room_id)
        expected = str(expected_sha256 or "").strip().lower()
        if expected and not _SHA256_RE.fullmatch(expected):
            raise ValueError("agent file preview expected sha256 is invalid")
        if expected and expected != str(receipt["sha256"]):
            raise ValueError("agent file preview digest does not match its block receipt")

        descriptor = file_descriptor(receipt, session_id=session_id)
        kind = str(descriptor["previewKind"])
        content: str | None = None
        preview_byte_size = 0
        truncated = False
        if kind in _TEXT_PREVIEW_KINDS:
            # AgentMediaStore already verified the full immutable blob before this
            # bounded projection is made.
            raw.decode("utf-8")
            truncated = len(raw) > MAX_TEXT_PREVIEW_BYTES
            preview_bytes = raw[:MAX_TEXT_PREVIEW_BYTES]
            content = preview_bytes.decode("utf-8", errors="ignore")
            preview_byte_size = len(content.encode("utf-8"))

        payload: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-file-preview.v1",
            "descriptor": descriptor,
            "content": content,
            "previewByteSize": preview_byte_size,
            "truncated": truncated,
        }
        validate_contract(payload, "agent-file-preview.v1.json")
        return payload


def file_descriptor(receipt: Mapping[str, object], *, session_id: str = "") -> dict[str, object]:
    media_id = str(receipt.get("mediaId") or "")
    session_id = str(receipt.get("sessionId") or session_id)
    file_name = str(receipt.get("fileName") or "attachment")
    mime_type = str(receipt.get("mimeType") or "application/octet-stream")
    preview_kind = preview_kind_for(file_name=file_name, mime_type=mime_type)
    descriptor: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-file-descriptor.v1",
        "mediaId": media_id,
        "sessionId": session_id,
        "fileName": file_name,
        "mimeType": mime_type,
        "byteSize": int(receipt.get("byteSize") or 0),
        "sha256": str(receipt.get("sha256") or ""),
        "previewKind": preview_kind,
        "language": language_for(file_name) if preview_kind == "code" else "",
        "contentUrl": (
            f"/api/agent/media/{quote(media_id, safe='')}/content"
            f"?sessionId={quote(session_id, safe='')}"
        ),
    }
    validate_contract(descriptor, "agent-file-descriptor.v1.json")
    return descriptor


def preview_kind_for(*, file_name: str, mime_type: str) -> str:
    normalized_mime = str(mime_type).split(";", 1)[0].strip().lower()
    suffix = Path(file_name).suffix.lower()
    if normalized_mime in IMAGE_MIME_TYPES:
        return "image"
    if normalized_mime == "text/markdown" or suffix in {".md", ".markdown", ".mdx"}:
        return "markdown"
    if normalized_mime == "text/html" or suffix in {".html", ".htm"}:
        return "html"
    if normalized_mime in {"text/x-diff", "text/x-patch"} or suffix in {".diff", ".patch"}:
        return "diff"
    if normalized_mime in TEXT_MEDIA_MIME_TYPES:
        return "code"
    return "unsupported"


def language_for(file_name: str) -> str:
    name = Path(file_name).name.lower()
    if name in {"dockerfile", "containerfile"}:
        return "dockerfile"
    if name in {"makefile", "gnumakefile"}:
        return "makefile"
    return _LANGUAGE_BY_EXTENSION.get(Path(name).suffix.lower(), "text")
