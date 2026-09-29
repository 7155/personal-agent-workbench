"""Local EPUB spine extraction; chapter anchors are not printed page numbers."""
from __future__ import annotations

import hashlib
import html
import posixpath
import zipfile
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from .mineru_structure import _TableRows
from .models import DocumentParseError, ParsedAsset, ParsedBlock, ParsedDocument


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        raw = archive.read(name)
        upper = raw.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise DocumentParseError("EPUB manifest declarations are not allowed", code="unsafe_archive")
        return ET.fromstring(raw)
    except (KeyError, ET.ParseError) as exc:
        raise DocumentParseError(f"EPUB manifest is missing or invalid: {name}", code="epub_invalid_package") from exc


def _resolve(owner: str, href: str) -> str:
    parsed = urlsplit(href)
    target = unquote(parsed.path).replace("\\", "/")
    if parsed.scheme or parsed.netloc or target.startswith("/") or not target:
        raise DocumentParseError("EPUB references a non-local resource", code="epub_external_resource")
    resolved = posixpath.normpath(posixpath.join(str(PurePosixPath(owner).parent), target))
    if resolved == ".." or resolved.startswith("../") or "\x00" in resolved or ":" in resolved:
        raise DocumentParseError("EPUB resource escapes its package", code="unsafe_archive")
    return resolved


class _Chapter(HTMLParser):
    def __init__(self, source: str, order: int, images: dict[str, ParsedAsset]):
        super().__init__(convert_charrefs=True)
        self.source, self.order, self.images = source, order, images
        self.blocks: list[ParsedBlock] = []
        self.parts: list[str] = []
        self.headings: dict[int, str] = {}
        self.level = 0
        self.ignored = 0
        self.table_depth = 0
        self.table_parts: list[str] = []

    def emit(self, kind: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        if len(self.blocks) >= 100_000:
            raise DocumentParseError("EPUB chapter has too many blocks", code="unsafe_archive")
        self.blocks.append(ParsedBlock(kind, text, heading_path=tuple(self.headings.values()),
                                       metadata={"sourcePart": self.source, "spineOrdinal": self.order, **(metadata or {})}))

    def flush(self) -> None:
        text = "".join(self.parts).strip()
        self.parts.clear()
        if text:
            if self.level:
                self.headings = {key: value for key, value in self.headings.items() if key < self.level}
                self.headings[self.level] = text
            self.emit("heading" if self.level else "paragraph", text, {"headingLevel": self.level} if self.level else {})

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"head", "script", "style"}:
            self.ignored += 1
        if self.ignored:
            return
        if tag == "table" or self.table_depth:
            if not self.table_depth:
                self.flush()
            if tag == "table":
                self.table_depth += 1
            self.table_parts.append(self.get_starttag_text() or f"<{tag}>")
            return
        if tag in {"p", "div", "section", "li", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush()
            self.level = int(tag[1]) if len(tag) == 2 and tag.startswith("h") else 0
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "img":
            self.flush()
            values = dict(attrs)
            alt = values.get("alt") or values.get("title") or ""
            href = values.get("src") or ""
            metadata: dict[str, Any] = {"ocrApplied": False, "textSource": "embedded-alt-text" if alt else "asset-placeholder"}
            if urlsplit(href).scheme or urlsplit(href).netloc:
                metadata["externalImageSkipped"] = True
            else:
                target = _resolve(self.source, href)
                asset = self.images.get(target)
                if asset is None:
                    raise DocumentParseError(f"EPUB image is missing from manifest: {target}", code="epub_invalid_package")
                metadata.update({"assetName": asset.name, "assetSha256": asset.sha256, "imagePath": target})
            self.emit("image", alt or "[Embedded image; no OCR text]", metadata)

    def handle_endtag(self, tag: str) -> None:
        if self.ignored:
            if tag in {"head", "script", "style"}:
                self.ignored -= 1
            return
        if self.table_depth:
            self.table_parts.append(f"</{tag}>")
            if tag == "table":
                self.table_depth -= 1
                if not self.table_depth:
                    table = "".join(self.table_parts)
                    self.table_parts.clear()
                    parser = _TableRows()
                    parser.feed(table)
                    rows = parser.expanded_rows()
                    metadata: dict[str, Any] = {"tableHtml": table}
                    if rows:
                        metadata["rows"] = rows
                    if parser.fallback_reason:
                        metadata["tableRowsFallbackReason"] = parser.fallback_reason
                    self.emit("table", table, metadata)
            return
        if tag in {"p", "div", "section", "li", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "body"}:
            self.flush()
            self.level = 0

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            if self.table_depth:
                self.table_parts.append(html.escape(data))
            else:
                self.parts.append(data)


def parse_epub_archive(archive: zipfile.ZipFile, entries: set[str], *, title: str) -> ParsedDocument:
    container = _xml(archive, "META-INF/container.xml")
    rootfile = next((node for node in container.iter() if _tag(node) == "rootfile"), None)
    if rootfile is None:
        raise DocumentParseError("EPUB has no root package", code="epub_invalid_package")
    package_name = _resolve("container.xml", rootfile.get("full-path", ""))
    package = _xml(archive, package_name)
    manifest: dict[str, tuple[str, str]] = {}
    images: dict[str, ParsedAsset] = {}
    for node in package.iter():
        if _tag(node) != "item":
            continue
        identifier = node.get("id", "")
        if not identifier or identifier in manifest:
            raise DocumentParseError("EPUB manifest identity is invalid", code="epub_invalid_package")
        # Remote resources are not fetched; a spine reference to one fails below.
        href = node.get("href", "")
        if urlsplit(href).scheme or urlsplit(href).netloc:
            continue
        target = _resolve(package_name, href)
        media_type = node.get("media-type", "")
        manifest[identifier] = (target, media_type)
        if media_type.startswith("image/"):
            if target not in entries:
                raise DocumentParseError("EPUB image resource is missing", code="epub_invalid_package")
            data = archive.read(target)
            images[target] = ParsedAsset(PurePosixPath(target).name, media_type, hashlib.sha256(data).hexdigest(), data)
    spine = next((node for node in package.iter() if _tag(node) == "spine"), None)
    if spine is None or not len(spine):
        raise DocumentParseError("EPUB has no reading spine", code="epub_invalid_package")
    encrypted = set()
    if "META-INF/encryption.xml" in entries:
        encrypted = {unquote(node.get("URI", "")) for node in _xml(archive, "META-INF/encryption.xml").iter() if _tag(node) == "CipherReference"}
    if encrypted.intersection(images):
        raise DocumentParseError("EPUB image assets are encrypted", code="epub_encrypted")
    blocks: list[ParsedBlock] = []
    chapter_sources: list[str] = []
    for ordinal, item in enumerate(spine):
        reference = manifest.get(item.get("idref", ""))
        if reference is None or reference[0] not in entries:
            raise DocumentParseError("EPUB spine references a missing local part", code="epub_invalid_package")
        source, media_type = reference
        if source in encrypted:
            raise DocumentParseError("EPUB chapter is encrypted", code="epub_encrypted")
        if media_type not in {"application/xhtml+xml", "text/html"}:
            raise DocumentParseError(f"EPUB spine media type requires another adapter: {media_type}", code="epub_unsupported_spine")
        raw = archive.read(source)
        try:
            body = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        except UnicodeError as exc:
            raise DocumentParseError("EPUB XHTML must use UTF-8 or BOM-marked UTF-16", code="epub_invalid_package") from exc
        if b"<!ENTITY" in raw.replace(b"\x00", b"").upper():
            raise DocumentParseError("EPUB entities are not supported", code="unsafe_archive")
        chapter = _Chapter(source, ordinal, images)
        chapter.feed(body)
        chapter.close()
        chapter.flush()
        if chapter.table_depth:
            raise DocumentParseError("EPUB contains an unclosed table", code="epub_invalid_package")
        if len(blocks) + len(chapter.blocks) > 100_000:
            raise DocumentParseError("EPUB has too many structural blocks", code="unsafe_archive")
        blocks.extend(chapter.blocks)
        chapter_sources.append(source)
    if not blocks:
        raise DocumentParseError("EPUB contains no readable content", code="empty_document")
    book_title = next(("".join(node.itertext()).strip() for node in package.iter() if _tag(node) == "title"), "") or title
    return ParsedDocument(text="\n\n".join(block.text for block in blocks), title=book_title, provider="builtin",
                          provider_version="epub-spine-v1", assets=tuple(images.values()), blocks=tuple(blocks),
                          metadata={"structureVersion": "epub-spine-v1", "spine": chapter_sources, "pageNumbersAvailable": False})
