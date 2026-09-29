"""Parser evidence blocks to bounded retrieval spans, independent of persistence."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import Any, Iterator

from .models import ParsedBlock, ParsedDocument


_ATOMIC = {"table", "chart", "formula", "equation", "image", "figure", "caption"}
_REFERENCE = re.compile(r"^(?:references|bibliography|参考文献|参考资料)\s*[:：]?\s*$", re.I)
_SECTION = re.compile(r"^(?:abstract|摘要|references|bibliography|参考文献|\d+(?:\.\d+)*[.、]?\s+\S.{0,140})\s*$", re.I)
MAX_SOURCE_BLOCKS = 8


def _bounded_text(value: str, budget: int) -> str:
    return value.encode("utf-8")[:budget].decode("utf-8", errors="ignore")


def _bounded_path(path: tuple[str, ...]) -> tuple[str, ...]:
    result = []
    remaining = 512
    for heading in path[:8]:
        value = _bounded_text(heading, min(128, remaining))
        if not value:
            break
        result.append(value)
        remaining -= len(value.encode("utf-8"))
    return tuple(result)


def _source_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("coordinateSystem", "assetSha256", "ocrApplied", "chartDataAvailable", "textSource", "spineOrdinal", "sourcePart", "sheetName", "slideNumber", "rowStart", "rowEnd",
                "columnStart", "columnEnd", "assetName", "imagePath", "structureSource", "tableRowsFallbackReason"):
        value = metadata.get(key)
        if isinstance(value, str):
            value = _bounded_text(value, 160)
        elif type(value) not in (int, float, bool) and value is not None:
            continue
        if value is None:
            continue
        candidate = {**result, key: value}
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) <= 256:
            result = candidate
    return result


def serialize_blocks(blocks: tuple[ParsedBlock, ...]) -> list[dict[str, Any]]:
    return [asdict(block) for block in blocks]


def restore_blocks(value: Any) -> tuple[ParsedBlock, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(ParsedBlock(
        kind=row["kind"], text=row["text"], page=row.get("page"),
        bbox=tuple(row["bbox"]) if row.get("bbox") is not None else None,
        heading_path=tuple(row.get("heading_path") or ()), metadata=dict(row.get("metadata") or {}),
    ) for row in value if isinstance(row, dict) and isinstance(row.get("text"), str) and isinstance(row.get("kind"), str))


def paper_blocks(parsed: ParsedDocument) -> tuple[ParsedBlock, ...]:
    """Text-only papers preserve explicit sections; no author/geometry guesses."""
    result: list[ParsedBlock] = []
    heading = ""
    buffer: list[str] = []
    pages = parsed.text.split("\f")
    paginated = len(pages) > 1 or parsed.metadata.get("pageSeparator") == "\f"
    def flush(page_index: int, current_heading: str) -> None:
        if buffer:
            result.append(ParsedBlock(
                "reference" if _REFERENCE.fullmatch(current_heading) else "paragraph",
                "\n".join(buffer).strip(), page=page_index if paginated else None,
                heading_path=(current_heading,) if current_heading else (),
                metadata={"structureSource": "text-headings"},
            ))
            buffer.clear()

    for page_index, text in enumerate(pages, 1):
        for line in text.splitlines():
            clean = line.strip()
            markdown = re.match(r"^#{1,6}\s+(.+?)\s*#*$", clean)
            # Numbered bibliography entries are evidence, not section titles.
            section = bool(_SECTION.fullmatch(clean)) and not _REFERENCE.fullmatch(heading)
            if markdown or section:
                flush(page_index, heading)
                heading = markdown.group(1) if markdown else clean
                buffer.append(line)
            elif clean or buffer:
                buffer.append(line)
        flush(page_index, heading)
    return tuple(result)


def _split_text(text: str, budget: int, overlap: int = 0) -> Iterator[str]:
    remaining = text.strip()
    while remaining:
        end = min(len(remaining), budget)
        if end < len(remaining):
            boundary = max(remaining.rfind("\n", budget // 2, end), remaining.rfind(" ", budget // 2, end))
            if boundary > overlap:
                end = boundary
        yield remaining[:end].strip()
        if end == len(remaining):
            break
        remaining = remaining[max(1, end - overlap):].lstrip()


def _table_parts(block: ParsedBlock, budget: int) -> Iterator[str]:
    text = block.text.strip()
    if len(text) <= budget:
        yield text
        return
    lines = text.splitlines()
    header: list[str] = []
    if len(lines) > 1 and "|" in lines[0] and re.fullmatch(r"[\s|:\-]+", lines[1]):
        header, lines = lines[:2], lines[2:]
    elif isinstance(block.metadata.get("rows"), list):
        rows = block.metadata["rows"]
        def row_text(row: Any) -> str:
            return "| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in row) + " |"
        if rows and all(isinstance(row, list) for row in rows):
            # Captions/footnotes accompany MinerU HTML. Retain them when the
            # oversized table is represented as bounded Markdown row groups.
            html = block.metadata.get("tableHtml")
            if isinstance(html, str):
                html = html.strip()
            if isinstance(html, str) and html and html in text:
                extra = text.replace(html, "", 1).strip()
                yield from _split_text(extra, budget)
            lines = [row_text(row) for row in rows]
            header, lines = [lines[0], "| " + " | ".join("---" for _ in rows[0]) + " |"], lines[1:]
    prefix = "\n".join(header)
    if len(prefix) >= budget // 2:
        # A single oversized header/row cannot stay atomic. Preserve its text
        # under a recorded split, rather than exceeding the context budget.
        yield from _split_text(text, budget)
        return
    current = prefix
    for line in lines:
        if len(current) + len(line) + 1 <= budget:
            current = f"{current}\n{line}".strip()
            continue
        if current and current != prefix:
            yield current
        room = budget - len(prefix) - (1 if prefix else 0)
        fragments = list(_split_text(line, room))
        for fragment in fragments[:-1]:
            yield f"{prefix}\n{fragment}".strip()
        current = f"{prefix}\n{fragments[-1]}".strip() if fragments else prefix
    if current:
        yield current


def structured_spans(parsed: ParsedDocument, config: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = parsed.blocks if any(block.text.strip() for block in parsed.blocks) else paper_blocks(parsed)
    budget = int(config["size"])
    spans: list[dict[str, Any]] = []
    heading_path: tuple[str, ...] = ()
    pending: dict[str, Any] | None = None
    pending_path: tuple[str, ...] = ()

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            spans.append(pending)
            pending = None

    for index, block in enumerate(blocks):
        text = block.text.strip()
        # Printed page numbers remain in the stored parse tree; indexing them
        # as standalone evidence creates numeric search noise.
        if not text or block.kind == "page_number":
            continue
        if block.heading_path:
            heading_path = block.heading_path
        elif block.kind in {"heading", "title"}:
            heading_path = (text.lstrip("# "),)
        kind = "reference" if heading_path and _REFERENCE.fullmatch(heading_path[-1]) else block.kind
        bounded_path = _bounded_path(heading_path)
        source = {
            "id": hashlib.sha256(f"{index}:{block.page}:{text}".encode()).hexdigest()[:24],
            "order": index, "kind": _bounded_text(block.kind, 64), "page": block.page,
            "bbox": list(block.bbox) if block.bbox is not None else None,
            "metadata": _source_metadata(block.metadata),
        }
        overlap = int(config["overlap"]) if kind not in _ATOMIC else 0
        parts = list(_table_parts(block, budget) if kind in {"table", "chart"} else _split_text(text, budget, overlap))
        for part_index, part in enumerate(parts):
            provenance = {
                "kind": _bounded_text(kind, 64), "headingPath": list(bounded_path), "sourceBlocks": [source],
                "headingTruncated": bounded_path != heading_path,
                "parser": _bounded_text(parsed.provider, 160), "parserVersion": _bounded_text(parsed.provider_version, 160),
                "split": len(parts) > 1, "part": part_index + 1, "parts": len(parts),
            }
            candidate = {"content": part, "heading": " > ".join(bounded_path), "page": block.page, "provenance": provenance}
            atomic = kind in _ATOMIC or len(parts) > 1
            same_group = pending is not None and (not config["respectHeadings"] or pending_path == heading_path) and pending["provenance"]["kind"] == kind
            same_page = pending is not None and pending["page"] == block.page
            if not atomic and same_group and len(pending["provenance"]["sourceBlocks"]) < MAX_SOURCE_BLOCKS and (same_page or not config["respectPageBoundaries"]) and len(pending["content"]) + len(part) + 2 <= budget:
                pending["content"] += "\n\n" + part
                pending["provenance"]["sourceBlocks"].append(source)
                if not same_page:
                    pending["page"] = None
            else:
                flush()
                if atomic:
                    spans.append(candidate)
                else:
                    pending = candidate
                    pending_path = heading_path
    flush()
    return spans
