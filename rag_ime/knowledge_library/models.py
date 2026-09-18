from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


KNOWLEDGE_SCHEMA_VERSION = "rag-ime.knowledge-library.v1"
PARSER_MODES = frozenset({"auto", "builtin", "mineru"})
DOCUMENT_STATES = frozenset({"queued", "parsing", "indexing", "ready", "stale", "failed", "deleting"})
JOB_STATES = frozenset({"queued", "running", "succeeded", "failed", "cancelled", "superseded"})
DEFAULT_MINERU_TIMEOUT_SECONDS = 7_200.0


class KnowledgeLibraryError(RuntimeError):
    def __init__(self, message: str, *, code: str = "knowledge_library_error"):
        super().__init__(message)
        self.code = code


class KnowledgeNotFoundError(KnowledgeLibraryError):
    def __init__(self, message: str):
        super().__init__(message, code="not_found")


class KnowledgeConflictError(KnowledgeLibraryError):
    def __init__(self, message: str):
        super().__init__(message, code="conflict")


class DocumentParseError(KnowledgeLibraryError):
    def __init__(self, message: str, *, code: str = "parse_failed"):
        super().__init__(message, code=code)


@dataclass(frozen=True)
class KnowledgeLibraryConfig:
    root_dir: Path
    database_name: str = "knowledge.sqlite"
    chunk_chars: int = 1_200
    chunk_overlap_chars: int = 160
    max_source_bytes: int = 200 * 1024 * 1024
    max_asset_read_bytes: int = 25 * 1024 * 1024
    max_source_preview_bytes: int = 50 * 1024 * 1024
    mineru_enabled: bool = False
    mineru_port: int = 30_001
    # MinerU is a local, synchronous HTTP adapter.  Large scanned theses can
    # legitimately take longer than 30 minutes; keep the deadline explicit in
    # the runtime config instead of treating a long parse as an unavailable
    # service.
    mineru_timeout_seconds: float = DEFAULT_MINERU_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        root = Path(self.root_dir).expanduser()
        object.__setattr__(self, "root_dir", root)
        if not 1_024 <= int(self.mineru_port) <= 65_535:
            raise ValueError("mineru_port must be between 1024 and 65535")
        if float(self.mineru_timeout_seconds) < 1.0:
            raise ValueError("mineru_timeout_seconds must be at least 1 second")
        if self.chunk_chars < 200:
            raise ValueError("chunk_chars must be at least 200")
        if not 0 <= self.chunk_overlap_chars < self.chunk_chars:
            raise ValueError("chunk_overlap_chars must be smaller than chunk_chars")

    @property
    def database_path(self) -> Path:
        return self.root_dir / self.database_name

    @property
    def files_dir(self) -> Path:
        return self.root_dir / "files"

    @property
    def assets_dir(self) -> Path:
        return self.root_dir / "assets"

    @property
    def artifacts_dir(self) -> Path:
        return self.root_dir / "artifacts"


@dataclass(frozen=True)
class ParsedAsset:
    name: str
    media_type: str
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    provider: str
    provider_version: str = "1"
    title: str = ""
    assets: tuple[ParsedAsset, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AssetBlob:
    asset_id: str
    file_name: str
    media_type: str
    data: bytes = field(repr=False)

    @property
    def byte_size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    base_id: str
    base_name: str
    document_id: str
    document_name: str
    ordinal: int
    content: str
    score: float
    page: int | None = None
    heading: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "chunkId": self.chunk_id,
            "baseId": self.base_id,
            "baseName": self.base_name,
            "documentId": self.document_id,
            "documentName": self.document_name,
            "ordinal": self.ordinal,
            "content": self.content,
            "score": self.score,
            "citation": {
                "documentId": self.document_id,
                "documentName": self.document_name,
                "page": self.page,
                "heading": self.heading or None,
                "chunkId": self.chunk_id,
            },
        }
        if self.diagnostics:
            result["diagnostics"] = dict(self.diagnostics)
        return result
