"""Lifecycle-owned Knowledge instance with explicit space storage and no keys."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import io
from pathlib import Path
from threading import Thread
from typing import Any
import zipfile

from ..knowledge_library.client import HttpKnowledgeClient
from ..knowledge_library.dense import NullDenseIndex
from ..knowledge_library.models import DocumentParseError, KnowledgeLibraryConfig
from ..knowledge_library.parsers import (
    BuiltinDocumentParser,
    ParserRouter,
    ZipSafetyLimits,
)
from ..knowledge_library.service import KnowledgeLibraryService
from ..knowledge_library.worker import KnowledgeWorkerServer, _database_intake_validator


TEAM_KNOWLEDGE_MAX_SOURCE_BYTES = 8 * 1024 * 1024
TEAM_KNOWLEDGE_MAX_ASSET_BYTES = TEAM_KNOWLEDGE_MAX_SOURCE_BYTES
TEAM_KNOWLEDGE_MAX_SOURCE_PREVIEW_BYTES = TEAM_KNOWLEDGE_MAX_SOURCE_BYTES
TEAM_KNOWLEDGE_MAX_ARCHIVE_ENTRIES = 256
TEAM_KNOWLEDGE_MAX_ARCHIVE_ENTRY_BYTES = 8 * 1024 * 1024
TEAM_KNOWLEDGE_MAX_ARCHIVE_EXPANDED_BYTES = 32 * 1024 * 1024

_OFFICE_EXTENSIONS = frozenset({".docx", ".pptx", ".xlsx"})


def validate_team_document_bytes(data: bytes, file_name: str) -> None:
    """Reject sources whose parser work could exceed the team worker budget.

    The normal Knowledge parser already performs safety checks for Office
    packages and MinerU responses, but its defaults are intentionally larger
    for a local personal installation.  Team imports use this smaller
    preflight before the intake receipt is persisted and repeat the same
    checks in the worker parser so a caller cannot bypass the Team facade.
    PDF parsing is deliberately disabled for Team imports.  The shared
    in-process PDF parser has no trustworthy hard output bound for every
    legal filter/object encoding; accepting it here would turn the 8 MiB input
    bound into a false safety claim.  A future isolated parser may widen this
    explicit product boundary.
    """

    payload = bytes(data)
    if len(payload) > TEAM_KNOWLEDGE_MAX_SOURCE_BYTES:
        raise DocumentParseError("source file exceeds the team 8 MiB limit", code="source_too_large")
    suffix = Path(str(file_name)).suffix.lower()
    if suffix in _OFFICE_EXTENSIONS:
        _validate_office_archive(io.BytesIO(payload))
    elif suffix == ".pdf":
        raise DocumentParseError(
            "PDF parsing is not enabled in team spaces",
            code="pdf_not_allowed",
        )


class _TeamBuiltinDocumentParser(BuiltinDocumentParser):
    """Builtin parser with the smaller Team archive/PDF budgets."""

    def parse(self, path: Path):  # type: ignore[no-untyped-def]
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise DocumentParseError("document source is unavailable", code="source_unavailable") from exc
        if size > TEAM_KNOWLEDGE_MAX_SOURCE_BYTES:
            raise DocumentParseError("source file exceeds the team 8 MiB limit", code="source_too_large")
        suffix = path.suffix.lower()
        if suffix in _OFFICE_EXTENSIONS:
            try:
                with path.open("rb") as source:
                    _validate_office_archive(source)
            except DocumentParseError:
                raise
            except OSError as exc:
                raise DocumentParseError("Office document is unavailable", code="source_unavailable") from exc
        elif suffix == ".pdf":
            raise DocumentParseError(
                "PDF parsing is not enabled in team spaces",
                code="pdf_not_allowed",
            )
        return super().parse(path)


class _TeamParserRouter(ParserRouter):
    def __init__(self, config: KnowledgeLibraryConfig) -> None:
        # mineru_enabled remains false.  The explicit router is still useful:
        # it ensures that a future shared default parser cannot silently widen
        # the Team worker's archive/PDF budget.
        super().__init__(config, builtin=_TeamBuiltinDocumentParser())


def _validate_office_archive(source: io.BufferedIOBase | io.BytesIO) -> None:
    limits = ZipSafetyLimits(
        max_zip_bytes=TEAM_KNOWLEDGE_MAX_SOURCE_BYTES,
        max_entries=TEAM_KNOWLEDGE_MAX_ARCHIVE_ENTRIES,
        max_entry_bytes=TEAM_KNOWLEDGE_MAX_ARCHIVE_ENTRY_BYTES,
        max_expanded_bytes=TEAM_KNOWLEDGE_MAX_ARCHIVE_EXPANDED_BYTES,
        max_compression_ratio=100.0,
    )
    try:
        archive = zipfile.ZipFile(source, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentParseError("Office document is not a valid package", code="unsafe_archive") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_entries:
            raise DocumentParseError("Office package has too many entries", code="unsafe_archive")
        expanded_total = 0
        names: set[str] = set()
        for info in infos:
            raw_name = str(info.filename).replace("\\", "/")
            if not raw_name or raw_name.startswith("/") or ".." in Path(raw_name).parts:
                raise DocumentParseError("Office package contains an unsafe path", code="unsafe_archive")
            mode = (int(info.external_attr) >> 16) & 0o170000
            if mode == 0o120000:
                raise DocumentParseError("Office package symlinks are not allowed", code="unsafe_archive")
            if raw_name in names:
                raise DocumentParseError("Office package contains duplicate entries", code="unsafe_archive")
            names.add(raw_name)
            if info.file_size > limits.max_entry_bytes:
                raise DocumentParseError("Office package entry exceeds the team size limit", code="unsafe_archive")
            if info.compress_size > 0 and info.file_size / info.compress_size > limits.max_compression_ratio:
                raise DocumentParseError("Office package compression ratio exceeds the team limit", code="unsafe_archive")
            expanded_total += int(info.file_size)
            if expanded_total > limits.max_expanded_bytes:
                raise DocumentParseError("Office package expanded size exceeds the team limit", code="unsafe_archive")
            # Embedded archives are not needed by the builtin XML projection and
            # can create a second unbounded parser surface.
            if Path(raw_name).suffix.lower() in {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar"}:
                raise DocumentParseError("nested archives are not allowed", code="unsafe_archive")


class TeamKnowledgeWorker(HttpKnowledgeClient):
    def __init__(
        self,
        root: Path,
        *,
        settings_provider: Callable[[], dict[str, object]],
        intake_db_path: Path | None = None,
    ) -> None:
        self.root_dir = root
        self.settings_provider = settings_provider
        config = KnowledgeLibraryConfig(
            root_dir=root,
            max_source_bytes=TEAM_KNOWLEDGE_MAX_SOURCE_BYTES,
            max_asset_read_bytes=TEAM_KNOWLEDGE_MAX_ASSET_BYTES,
            max_source_preview_bytes=TEAM_KNOWLEDGE_MAX_SOURCE_PREVIEW_BYTES,
            mineru_enabled=False,
        )
        self.library = KnowledgeLibraryService(
            config,
            parser_router=_TeamParserRouter(config),
            dense_index=NullDenseIndex(), background_jobs=True,
        )
        self.server = KnowledgeWorkerServer(
            ('127.0.0.1', 0),
            self.library,
            owner='team-space:' + root.parent.name,
            intake_validator=(
                _database_intake_validator(intake_db_path)
                if intake_db_path is not None else None
            ),
        )
        super().__init__('http://127.0.0.1:' + str(self.server.server_port))
        self.thread = Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
        self.thread.start()

    def management_call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        if not operation.startswith('management_'):
            raise ValueError('Unsupported Knowledge operation')
        return getattr(super(), operation)(*args, **kwargs)

    def find(self, payload: Mapping[str, object]) -> dict[str, object]:
        # The Agent endpoint eventually reaches the library's stdlib-regex
        # compatibility path. Team keeps this operation literal-only so a
        # model cannot submit a catastrophic pattern through a lower seam.
        if payload.get('useRegex') is True or payload.get('regex') is True:
            raise DocumentParseError(
                'regular-expression document search is unavailable in team spaces',
                code='regex_not_allowed',
            )
        return super().find(payload)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.library.close()


__all__ = [
    'TEAM_KNOWLEDGE_MAX_SOURCE_BYTES',
    'TEAM_KNOWLEDGE_MAX_ASSET_BYTES',
    'TEAM_KNOWLEDGE_MAX_SOURCE_PREVIEW_BYTES',
    'TeamKnowledgeWorker',
    'validate_team_document_bytes',
]
