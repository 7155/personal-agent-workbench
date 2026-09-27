"""Normalize MinerU content-list evidence without inferring missing geometry.

The local MinerU 3.4 content-list contract uses zero-based page_idx and bbox
coordinates normalized to a 1000 by 1000 page. Original JSON is retained by
the archive adapter; this module keeps only useful block-level metadata.
"""

from __future__ import annotations

import json
import math
from html.parser import HTMLParser
from typing import Any

from .models import DocumentParseError, ParsedBlock


MAX_STRUCTURE_BYTES = 16 * 1024 * 1024
MAX_STRUCTURE_BLOCKS = 100_000
MAX_TABLE_ROWS = 2_000
MAX_TABLE_COLUMNS = 256
MAX_TABLE_CELLS = 100_000


class _TableRows(HTMLParser):
    """Keep HTML cell boundaries for oversized-table chunking; HTML stays canonical."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raw_rows: list[list[tuple[str, int, int]]] = []
        self.row: list[tuple[str, int, int]] | None = None
        self.cell: list[str] | None = None
        self.rowspan = self.colspan = 1
        self.depth = 0
        self.has_spans = False
        self.fallback_reason = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.depth += 1
        if self.depth != 1:
            return
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
            self.rowspan = self.colspan = 1
            for key, value in attrs:
                if key not in {"rowspan", "colspan"}:
                    continue
                if value != "1":
                    self.has_spans = True
                if value is None or not value.isascii() or not value.isdecimal() or len(value) > 4:
                    self.fallback_reason = "span_limit_or_invalid"
                    continue
                span = int(value)
                limit = MAX_TABLE_ROWS if key == "rowspan" else MAX_TABLE_COLUMNS
                if not 1 <= span <= limit:
                    self.fallback_reason = "span_limit_or_invalid"
                elif key == "rowspan":
                    self.rowspan = span
                else:
                    self.colspan = span
        elif tag == "br" and self.cell is not None:
            self.cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.depth == 1:
            if tag in {"td", "th"} and self.cell is not None and self.row is not None:
                if len(self.row) < MAX_TABLE_COLUMNS:
                    self.row.append(("".join(self.cell).strip(), self.rowspan, self.colspan))
                else:
                    self.fallback_reason = "table_grid_limit"
                self.cell = None
            elif tag == "tr" and self.row is not None:
                if len(self.raw_rows) < MAX_TABLE_ROWS:
                    self.raw_rows.append(self.row)
                else:
                    self.fallback_reason = "table_grid_limit"
                self.row = None
        if tag == "table":
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def expanded_rows(self) -> list[list[str]]:
        if self.fallback_reason:
            return []
        grid: dict[int, dict[int, str]] = {}
        cells = text_size = 0
        width = 0
        for row_index, row in enumerate(self.raw_rows):
            column = 0
            current = grid.setdefault(row_index, {})
            for text, rowspan, colspan in row:
                while column in current:
                    column += 1
                cells += rowspan * colspan
                text_size += len(text.encode("utf-8")) * rowspan * colspan
                if (row_index + rowspan > MAX_TABLE_ROWS or column + colspan > MAX_TABLE_COLUMNS
                        or cells > MAX_TABLE_CELLS or text_size > MAX_STRUCTURE_BYTES):
                    self.fallback_reason = "table_grid_limit"
                    return []
                for r in range(row_index, row_index + rowspan):
                    target = grid.setdefault(r, {})
                    for c in range(column, column + colspan):
                        if c in target:
                            self.fallback_reason = "overlapping_table_spans"
                            return []
                        target[c] = text
                column += colspan
                width = max(width, column)
        if len(grid) * width > MAX_TABLE_CELLS:
            self.fallback_reason = "table_grid_limit"
            return []
        return [[grid[r].get(c, "") for c in range(width)] for r in sorted(grid)]


def _invalid(message: str) -> DocumentParseError:
    return DocumentParseError(f"MinerU content list: {message}", code="mineru_invalid_structure")


def _text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return "\n".join(value)
    raise _invalid(f"{key} must be text or a list of text")


def parse_content_list(raw: bytes) -> tuple[tuple[ParsedBlock, ...], dict[str, Any]]:
    if len(raw) > MAX_STRUCTURE_BYTES:
        raise _invalid("JSON exceeds the structural size limit")
    try:
        content = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise _invalid("invalid UTF-8 or JSON") from exc
    if not isinstance(content, list):
        return (), {"structureStatus": "fallback", "structureFallbackReason": "unsupported_content_list_schema"}
    if len(content) > MAX_STRUCTURE_BLOCKS:
        raise _invalid("too many blocks")
    if not content:
        return (), {"structureStatus": "fallback", "structureFallbackReason": "content_list_empty"}

    blocks: list[ParsedBlock] = []
    headings: list[tuple[int, str]] = []
    unknown_types: set[str] = set()
    kinds = {"text": "paragraph", "title": "heading", "table": "table", "image": "image",
             "chart": "image", "equation": "equation", "code": "code", "list": "list",
             "reference": "reference", "header": "header", "footer": "footer",
             "page_number": "page_number", "discarded": "discarded"}
    for index, item in enumerate(content):
        if not isinstance(item, dict):
            raise _invalid(f"block {index} must be an object")
        source_type = item.get("type")
        if not isinstance(source_type, str) or not source_type:
            raise _invalid(f"block {index} has no type")
        kind = kinds.get(source_type, "unknown")
        meta: dict[str, Any] = {"sourceType": source_type, "sourceOrdinal": index}
        if kind == "unknown":
            unknown_types.add(source_type)
        page_idx = item.get("page_idx")
        if page_idx is not None and (type(page_idx) is not int or page_idx < 0):
            raise _invalid(f"block {index} has invalid page_idx")
        bbox = None
        raw_bbox = item.get("bbox")
        if raw_bbox is not None:
            try:
                valid_bbox = (isinstance(raw_bbox, list) and len(raw_bbox) == 4
                              and all(type(v) in (int, float) and math.isfinite(v) for v in raw_bbox))
            except OverflowError:
                valid_bbox = False
            if not valid_bbox:
                raise _invalid(f"block {index} has invalid bbox")
            x0, y0, x1, y1 = map(float, raw_bbox)
            bbox = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            meta["coordinateSystem"] = "normalized-1000"
            if x0 > x1 or y0 > y1:
                meta["bboxReordered"] = True

        text = _text(item, "text")
        if kind == "table":
            table = _text(item, "table_body")
            caption = _text(item, "table_caption")
            footnote = _text(item, "table_footnote")
            text = "\n".join(part for part in (caption, table, footnote) if part)
            meta["tableHtml"] = table
            meta["caption"] = caption
            row_parser = _TableRows()
            row_parser.feed(table)
            row_parser.close()
            rows = row_parser.expanded_rows()
            if rows:
                meta["rows"] = rows
            if row_parser.fallback_reason:
                meta["tableRowsFallbackReason"] = row_parser.fallback_reason
            if row_parser.has_spans:
                meta["tableSpansPresent"] = True
        elif kind == "image":
            prefix = "chart" if source_type == "chart" else "image"
            caption = _text(item, f"{prefix}_caption")
            text = "\n".join(part for part in (caption, _text(item, f"{prefix}_footnote"), text) if part)
            meta["caption"] = caption
        elif kind == "code":
            text = _text(item, "code_body") or text
            meta["caption"] = _text(item, "code_caption")
        elif kind == "list":
            text = _text(item, "list_items") or text
        image_path = item.get("img_path")
        if image_path is not None:
            if not isinstance(image_path, str):
                raise _invalid(f"block {index} has invalid img_path")
            # This is an identity only; never read a filesystem path from JSON.
            meta["imagePath"] = image_path
        level = item.get("text_level")
        if level is not None and (type(level) is not int or level < 0):
            raise _invalid(f"block {index} has invalid text_level")
        if kind in {"paragraph", "heading"} and (kind == "heading" or level):
            kind = "heading"
            level = level or 1
            meta["headingLevel"] = level
            while headings and headings[-1][0] >= level:
                headings.pop()
            if text:
                headings.append((level, text))
        blocks.append(ParsedBlock(kind=kind, text=text, page=page_idx + 1 if page_idx is not None else None,
                                  bbox=bbox, heading_path=tuple(title for _, title in headings), metadata=meta))
    metadata: dict[str, Any] = {"structureStatus": "partial" if unknown_types else "available",
                                "structureBlockCount": len(blocks), "structureSchema": "mineru-content-list-v1"}
    if unknown_types:
        metadata["unsupportedBlockTypes"] = sorted(unknown_types)[:32]
    return tuple(blocks), metadata
