from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import re
import socket
import urllib.error
import urllib.request
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol
from xml.etree import ElementTree

from .models import DocumentParseError, KnowledgeLibraryConfig, ParsedAsset, ParsedDocument


class DocumentParser(Protocol):
    def parse(self, path: Path) -> ParsedDocument:
        ...


_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".html", ".htm"})
_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"})
_OFFICE_EXTENSIONS = frozenset({".docx", ".pptx", ".xlsx"})


class BuiltinDocumentParser:
    """Small local parser lane. Scanned documents deliberately fall through to MinerU."""

    provider = "builtin"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in _TEXT_EXTENSIONS | _OFFICE_EXTENSIONS | {".pdf"}

    def parse(self, path: Path) -> ParsedDocument:
        suffix = path.suffix.lower()
        if suffix not in _TEXT_EXTENSIONS | _OFFICE_EXTENSIONS | {".pdf"}:
            raise DocumentParseError(f"unsupported built-in document type: {suffix or '<none>'}", code="unsupported_type")
        if suffix == ".pdf":
            text, engine, page_count = _extract_pdf_text(path)
            return ParsedDocument(
                text=text,
                title=path.stem,
                provider=self.provider,
                provider_version=f"pdf-{engine}-v1",
                metadata={"pageSeparator": "\f", "pageCount": page_count},
            )
        if suffix in _OFFICE_EXTENSIONS:
            text, engine = _extract_office_text(path, suffix=suffix)
            return ParsedDocument(
                text=text,
                title=path.stem,
                provider=self.provider,
                provider_version=f"{engine}-v1",
                metadata={"pageSeparator": "\\f"},
            )
        raw = path.read_bytes()
        text = _decode_text(raw)
        if suffix == ".json":
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass
        elif suffix in {".html", ".htm"}:
            text = _html_to_text(text)
        text = _normalize_text(text)
        if not text:
            raise DocumentParseError("document contains no readable text", code="empty_document")
        return ParsedDocument(text=text, title=path.stem, provider=self.provider, provider_version="text-v1")


@dataclass(frozen=True)
class ZipSafetyLimits:
    max_zip_bytes: int = 100 * 1024 * 1024
    max_entries: int = 500
    max_entry_bytes: int = 100 * 1024 * 1024
    max_expanded_bytes: int = 500 * 1024 * 1024
    max_compression_ratio: float = 200.0


class MinerULocalParser:
    """Loopback-only MinerU adapter with bounded, in-memory ZIP inspection."""

    provider = "mineru_local_http"

    def __init__(
        self,
        *,
        port: int = 30_001,
        timeout_seconds: float = 1_800.0,
        urlopen: Callable[..., Any] | None = None,
        zip_limits: ZipSafetyLimits | None = None,
    ):
        if not 1_024 <= int(port) <= 65_535:
            raise ValueError("MinerU port must be between 1024 and 65535")
        self.port = int(port)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.urlopen = urlopen or urllib.request.urlopen
        self.zip_limits = zip_limits or ZipSafetyLimits()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in _IMAGE_EXTENSIONS | {".pdf"}

    def health(self) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.base_url}/openapi.json", method="GET")
        try:
            with self.urlopen(request, timeout=min(5.0, self.timeout_seconds)) as response:
                if int(getattr(response, "status", 200)) != 200:
                    return {"status": "unhealthy", "port": self.port, "reason": "unexpected HTTP status"}
                payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
                available = "/file_parse" in payload.get("paths", {})
                return {
                    "status": "healthy" if available else "unhealthy",
                    "port": self.port,
                    "version": payload.get("info", {}).get("version", "unknown"),
                    "fileParseAvailable": available,
                }
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return {"status": "unavailable", "port": self.port, "reason": str(exc)}

    def parse(self, path: Path) -> ParsedDocument:
        if not self.supports(path):
            raise DocumentParseError(f"MinerU does not support {path.suffix.lower()}", code="unsupported_type")
        boundary = f"rag-ime-{uuid.uuid4().hex}"
        body = _multipart_body(
            boundary,
            path,
            {
                "lang_list": "ch",
                "backend": "hybrid-engine",
                "effort": "high" if path.suffix.lower() in _IMAGE_EXTENSIONS else "medium",
                "parse_method": "auto",
                "formula_enable": "true",
                "table_enable": "true",
                "image_analysis": "true",
                "start_page_id": "0",
                "end_page_id": "99999",
                "return_md": "true",
                "response_format_zip": "true",
                "return_images": "true",
            },
        )
        request = urllib.request.Request(
            f"{self.base_url}/file_parse",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/zip, application/octet-stream",
                "User-Agent": "rag-ime-knowledge/1.0",
            },
            method="POST",
        )
        try:
            with self.urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if status != 200:
                    detail = response.read(16_384).decode("utf-8", errors="replace")
                    raise DocumentParseError(f"MinerU returned HTTP {status}: {detail[:500]}", code="mineru_http_error")
                if content_type and not any(token in content_type for token in ("zip", "octet-stream")):
                    raise DocumentParseError(f"MinerU returned unexpected content type: {content_type}", code="mineru_bad_response")
                payload = response.read(self.zip_limits.max_zip_bytes + 1)
        except DocumentParseError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(64 * 1024).decode("utf-8", errors="replace")
            except OSError:
                detail = ""
            finally:
                exc.close()
            raise DocumentParseError(
                f"MinerU returned HTTP {exc.code}: {detail[:500]}",
                code="mineru_http_error",
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise DocumentParseError(
                f"MinerU parse timed out after {self.timeout_seconds:g} seconds",
                code="mineru_timeout",
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise DocumentParseError(
                    f"MinerU parse timed out after {self.timeout_seconds:g} seconds",
                    code="mineru_timeout",
                ) from exc
            raise DocumentParseError(f"MinerU is unavailable: {exc}", code="mineru_unavailable") from exc
        except OSError as exc:
            raise DocumentParseError(f"MinerU is unavailable: {exc}", code="mineru_unavailable") from exc
        if len(payload) > self.zip_limits.max_zip_bytes:
            raise DocumentParseError("MinerU ZIP response exceeds the compressed size limit", code="unsafe_archive")
        text, assets, archive_hash = inspect_mineru_zip(payload, limits=self.zip_limits)
        metadata: dict[str, Any] = {"archiveSha256": archive_hash, "port": self.port}
        if path.suffix.lower() == ".pdf":
            metadata["pageCount"] = _pdf_page_count(path)
        return ParsedDocument(
            text=text,
            title=path.stem,
            provider=self.provider,
            provider_version="file-parse-v1",
            assets=assets,
            metadata=metadata,
        )


class ParserRouter:
    def __init__(
        self,
        config: KnowledgeLibraryConfig,
        *,
        builtin: BuiltinDocumentParser | None = None,
        mineru: MinerULocalParser | None = None,
    ):
        self.config = config
        self.builtin = builtin or BuiltinDocumentParser()
        self.mineru = mineru or MinerULocalParser(
            port=config.mineru_port,
            timeout_seconds=config.mineru_timeout_seconds,
        )

    def parse(self, path: Path, *, mode: str = "auto") -> ParsedDocument:
        normalized = str(mode or "auto").strip().lower()
        if normalized not in {"auto", "builtin", "mineru"}:
            raise DocumentParseError(f"unknown parser mode: {mode}", code="invalid_parser_mode")
        if normalized == "builtin":
            return self.builtin.parse(path)
        if normalized == "mineru":
            self._require_mineru()
            return self.mineru.parse(path)
        if self.builtin.supports(path):
            try:
                parsed = self.builtin.parse(path)
            except DocumentParseError:
                if path.suffix.lower() != ".pdf" or not self.config.mineru_enabled:
                    raise
            else:
                if path.suffix.lower() != ".pdf" or _text_quality_is_acceptable(parsed.text):
                    return parsed
                if not self.config.mineru_enabled:
                    return parsed
        self._require_mineru()
        return self.mineru.parse(path)

    def _require_mineru(self) -> None:
        if not self.config.mineru_enabled:
            raise DocumentParseError(
                "this document needs MinerU, but the local MinerU parser is disabled",
                code="mineru_disabled",
            )


def inspect_mineru_zip(payload: bytes, *, limits: ZipSafetyLimits | None = None) -> tuple[str, tuple[ParsedAsset, ...], str]:
    limits = limits or ZipSafetyLimits()
    if len(payload) > limits.max_zip_bytes:
        raise DocumentParseError("archive exceeds compressed size limit", code="unsafe_archive")
    try:
        archive = zipfile.ZipFile(BytesIO(payload), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentParseError("MinerU response is not a valid ZIP archive", code="unsafe_archive") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_entries:
            raise DocumentParseError("archive has too many entries", code="unsafe_archive")
        expanded_total = 0
        for info in infos:
            _validate_zip_entry(info, limits=limits)
            expanded_total += info.file_size
            if expanded_total > limits.max_expanded_bytes:
                raise DocumentParseError("archive expanded size exceeds limit", code="unsafe_archive")
        markdown_infos = [item for item in infos if not item.is_dir() and item.filename.lower().endswith(".md")]
        if not markdown_infos:
            raise DocumentParseError("MinerU archive contains no Markdown output", code="mineru_bad_response")
        selected = next((item for item in markdown_infos if PurePosixPath(item.filename).name == "full.md"), markdown_infos[0])
        text = archive.read(selected).decode("utf-8", errors="strict")
        text = _normalize_text(text)
        if not text:
            raise DocumentParseError("MinerU returned empty Markdown", code="empty_document")
        assets: list[ParsedAsset] = []
        allowed_assets = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
        for info in infos:
            suffix = PurePosixPath(info.filename).suffix.lower()
            if info.is_dir() or suffix not in allowed_assets:
                continue
            data = archive.read(info)
            digest = hashlib.sha256(data).hexdigest()
            assets.append(
                ParsedAsset(
                    name=PurePosixPath(info.filename).name,
                    media_type=mimetypes.guess_type(info.filename)[0] or "application/octet-stream",
                    sha256=digest,
                    data=data,
                )
            )
    return text, tuple(assets), hashlib.sha256(payload).hexdigest()


def _extract_office_text(path: Path, *, suffix: str) -> tuple[str, str]:
    limits = ZipSafetyLimits(max_zip_bytes=200 * 1024 * 1024)
    if path.stat().st_size > limits.max_zip_bytes:
        raise DocumentParseError("Office document exceeds compressed size limit", code="unsafe_archive")
    try:
        archive = zipfile.ZipFile(path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentParseError("Office document is not a valid package", code="unsafe_archive") from exc
    with archive:
        entries = _safe_office_entries(archive, limits=limits)
        if suffix == ".docx":
            names = sorted(
                name
                for name in entries
                if re.fullmatch(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml", name)
            )
            pages = [_xml_text(_read_office_xml(archive, name)) for name in names]
            engine = "docx-xml"
        elif suffix == ".pptx":
            names = sorted(
                (name for name in entries if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=_numeric_archive_name,
            )
            pages = [_xml_text(_read_office_xml(archive, name)) for name in names]
            engine = "pptx-xml"
        else:
            pages = _xlsx_pages(archive, entries)
            engine = "xlsx-xml"
    text = _normalize_text("\f".join(page for page in pages if page.strip()))
    if not text:
        raise DocumentParseError("Office document contains no readable text", code="empty_document")
    return text, engine


def _safe_office_entries(archive: zipfile.ZipFile, *, limits: ZipSafetyLimits) -> set[str]:
    infos = archive.infolist()
    if len(infos) > limits.max_entries:
        raise DocumentParseError("Office package has too many entries", code="unsafe_archive")
    expanded_total = 0
    names: set[str] = set()
    for info in infos:
        raw_name = info.filename.replace("\\", "/")
        path = PurePosixPath(raw_name)
        mode = (info.external_attr >> 16) & 0o170000
        if not raw_name or raw_name.startswith("/") or path.is_absolute() or ".." in path.parts:
            raise DocumentParseError("Office package contains an unsafe path", code="unsafe_archive")
        if mode == 0o120000:
            raise DocumentParseError("Office package symlinks are not allowed", code="unsafe_archive")
        if info.file_size > limits.max_entry_bytes:
            raise DocumentParseError("Office package entry exceeds size limit", code="unsafe_archive")
        if info.compress_size > 0 and info.file_size / info.compress_size > limits.max_compression_ratio:
            raise DocumentParseError("Office package compression ratio exceeds limit", code="unsafe_archive")
        expanded_total += info.file_size
        if expanded_total > limits.max_expanded_bytes:
            raise DocumentParseError("Office package expanded size exceeds limit", code="unsafe_archive")
        names.add(raw_name)
    return names


def _read_office_xml(archive: zipfile.ZipFile, name: str) -> bytes:
    raw = archive.read(name)
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise DocumentParseError("Office XML declarations are not allowed", code="unsafe_archive")
    return raw


def _xml_text(raw: bytes) -> str:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise DocumentParseError("Office XML is malformed", code="parse_failed") from exc
    values = [str(node.text or "").strip() for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "t"]
    return "\n".join(value for value in values if value)


def _xlsx_pages(archive: zipfile.ZipFile, entries: set[str]) -> list[str]:
    shared: list[str] = []
    if "xl/sharedStrings.xml" in entries:
        raw = _read_office_xml(archive, "xl/sharedStrings.xml")
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise DocumentParseError("XLSX shared strings are malformed", code="parse_failed") from exc
        for item in root:
            shared.append(
                "".join(
                    str(node.text or "")
                    for node in item.iter()
                    if node.tag.rsplit("}", 1)[-1] == "t"
                )
            )
    sheet_names = sorted(
        (name for name in entries if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
        key=_numeric_archive_name,
    )
    pages: list[str] = []
    for name in sheet_names:
        raw = _read_office_xml(archive, name)
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise DocumentParseError("XLSX worksheet is malformed", code="parse_failed") from exc
        rows: list[str] = []
        for row in (node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "row"):
            cells: list[str] = []
            for cell in (node for node in row if node.tag.rsplit("}", 1)[-1] == "c"):
                value_node = next(
                    (node for node in cell.iter() if node.tag.rsplit("}", 1)[-1] in {"v", "t"}),
                    None,
                )
                value = str(value_node.text or "") if value_node is not None else ""
                if cell.attrib.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                cells.append(value.strip())
            if any(cells):
                rows.append("\t".join(cells))
        pages.append("\n".join(rows))
    return pages


def _numeric_archive_name(value: str) -> tuple[int, str]:
    match = re.search(r"(\d+)(?=\.xml$)", value)
    return (int(match.group(1)) if match else 0, value)


def _validate_zip_entry(info: zipfile.ZipInfo, *, limits: ZipSafetyLimits) -> None:
    raw_name = info.filename.replace("\\", "/")
    path = PurePosixPath(raw_name)
    if not raw_name or raw_name.startswith("/") or path.is_absolute() or ".." in path.parts:
        raise DocumentParseError(f"unsafe archive path: {raw_name!r}", code="unsafe_archive")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise DocumentParseError("archive symlinks are not allowed", code="unsafe_archive")
    if info.file_size > limits.max_entry_bytes:
        raise DocumentParseError("archive entry exceeds size limit", code="unsafe_archive")
    if info.compress_size > 0 and info.file_size / info.compress_size > limits.max_compression_ratio:
        raise DocumentParseError("archive compression ratio exceeds limit", code="unsafe_archive")
    if path.suffix.lower() in {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar"}:
        raise DocumentParseError("nested archives are not allowed", code="unsafe_archive")


def _multipart_body(boundary: str, path: Path, fields: dict[str, str]) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    safe_name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="files"; filename="{safe_name}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks)


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _html_to_text(source: str) -> str:
    source = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", source)
    source = re.sub(r"(?i)<br\s*/?>|</p\s*>|</h[1-6]\s*>|</li\s*>", "\n", source)
    return html.unescape(re.sub(r"(?s)<[^>]+>", " ", source))


def _normalize_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _text_quality_is_acceptable(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 200:
        return False
    readable = sum(character.isalnum() or "\u3400" <= character <= "\u9fff" for character in compact)
    return readable / max(1, len(compact)) >= 0.45


def _extract_pdf_text(path: Path) -> tuple[str, str, int]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        payload = path.read_bytes()
        text = _basic_pdf_text(payload)
        page_count = _basic_pdf_page_count(payload)
        engine = "basic"
    else:
        try:
            reader = PdfReader(str(path), strict=False)
            text = "\f".join((page.extract_text() or "").strip() for page in reader.pages)
            page_count = len(reader.pages)
        except Exception as exc:
            raise DocumentParseError(f"PDF parsing failed: {exc}", code="pdf_parse_failed") from exc
        engine = "pypdf"
    text = _normalize_text(text.replace("\f\n", "\f"))
    if not text:
        raise DocumentParseError("PDF contains no extractable text", code="pdf_needs_ocr")
    return text, engine, page_count


def _pdf_page_count(path: Path) -> int:
    """Read a PDF page count without trusting document-provided commands or scripts."""

    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        try:
            count = len(PdfReader(str(path), strict=False).pages)
            if 0 < count <= 100_000:
                return count
        except Exception:
            # MinerU may still parse PDFs whose cross-reference table is damaged.
            pass
    try:
        return _basic_pdf_page_count(path.read_bytes())
    except OSError:
        return 0


def _basic_pdf_page_count(payload: bytes) -> int:
    page_objects = len(re.findall(rb"/Type\s*/Page\b", payload))
    if 0 < page_objects <= 100_000:
        return page_objects
    counts = [int(value) for value in re.findall(rb"/Count\s+([0-9]{1,6})\b", payload)]
    plausible = [value for value in counts if 0 < value <= 100_000]
    return max(plausible, default=0)


def _basic_pdf_text(payload: bytes) -> str:
    streams: list[bytes] = [payload]
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", payload, flags=re.S):
        stream = match.group(1)
        try:
            streams.append(zlib.decompress(stream))
        except zlib.error:
            streams.append(stream)
    fragments: list[str] = []
    for stream in streams:
        for block in re.findall(rb"BT(.*?)ET", stream, flags=re.S):
            for token in re.findall(rb"\((?:\\.|[^\\)])*\)", block):
                decoded = _decode_pdf_literal(token[1:-1])
                if decoded.strip():
                    fragments.append(decoded)
            if fragments:
                fragments.append("\n")
    return " ".join(fragments)


def _decode_pdf_literal(value: bytes) -> str:
    value = re.sub(rb"\\([()\\])", rb"\1", value)
    value = value.replace(b"\\n", b"\n").replace(b"\\r", b"\n").replace(b"\\t", b"\t")
    value = re.sub(rb"\\[0-7]{1,3}", lambda match: bytes([int(match.group(0)[1:], 8) % 256]), value)
    return _decode_text(value)
