"""Bounded reader projections from persisted parser evidence; no I/O or inference."""
from __future__ import annotations

import json
import math
import re
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit


MAX_TABLES = 32
MAX_ROWS = 200
MAX_COLUMNS = 32
MAX_TOTAL_ROWS = 800
MAX_TOTAL_CELLS = 16_000
MAX_TABLE_BYTES = 512 * 1024
MAX_LOCATIONS = 32
MAX_TOTAL_LOCATIONS = 1024
MAX_LOCATION_BYTES = 512 * 1024


def _text(value: Any, chars: int = 500) -> str:
    return str(value)[:chars] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _bytes(value: str, maximum: int) -> str:
    return value.encode("utf-8")[:max(0, maximum)].decode("utf-8", errors="ignore")


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _page(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _blocks(value: Any):
    if isinstance(value, list):
        for index, block in enumerate(value):
            if isinstance(block, dict) and isinstance(block.get("kind"), str) and isinstance(block.get("text"), str):
                yield index, block, block["metadata"] if isinstance(block.get("metadata"), dict) else {}


def _html_header(value: str) -> bool:
    row = re.search(r"(?is)<tr\b[^>]*>(.*?)</tr\s*>", value[:256 * 1024])
    return bool(row and re.search(r"(?i)<th\b", row.group(1)))


def structured_tables(value: Any, extract: Callable[[str], list[dict[str, Any]]]) -> list[dict[str, Any]] | None:
    candidates: list[dict[str, Any]] = []
    for order, block, metadata in _blocks(value):
        if block["kind"] not in {"table", "chart"}:
            continue
        if len(candidates) == MAX_TABLES:
            candidates[-1].setdefault("truncationReasons", []).append("tables")
            break
        body = block["text"]
        parsed = extract(body[:256 * 1024])
        first = parsed[0] if parsed else {}
        reasons: list[str] = []
        if len(body) > 256 * 1024:
            reasons.append("scan")
        raw_rows = metadata.get("rows")
        columns: list[Any] = []
        if isinstance(raw_rows, list) and all(isinstance(row, list) for row in raw_rows):
            # OOXML worksheets/Word provide data rows plus a generated Markdown
            # header. HTML rows carry no reliable header unless <th> is present;
            # never silently consume the first data row as a column heading.
            markdown_header = bool(re.search(r"(?m)^\s*\|?.+\|.*\n\s*\|?\s*:?-{3,}", body[:16384]))
            if markdown_header and first:
                columns = first.get("columns") or []
                rows = raw_rows
            elif raw_rows and (block["kind"] == "chart" or _html_header(str(metadata.get("tableHtml") or body))):
                columns, rows = raw_rows[0], raw_rows[1:]
            else:
                width = max((len(row) for row in raw_rows), default=0)
                columns, rows = [f"Column {i + 1}" for i in range(min(width, MAX_COLUMNS))], raw_rows
                if width > MAX_COLUMNS:
                    reasons.append("columns")
            total = len(rows)
        else:
            columns, rows = first.get("columns") or [], first.get("rows") or []
            total = first.get("totalRowCount", len(rows) if first else None)
            reasons.extend(first.get("truncationReasons") or [])
        path = block.get("heading_path")
        heading = path[-1] if isinstance(path, (tuple, list)) and path and isinstance(path[-1], str) else ""
        title = metadata.get("caption") or heading or first.get("title") or f"{'图表' if block['kind'] == 'chart' else '表格'} {len(candidates) + 1}"
        candidates.append({
            "tableId": f"table-block-{order}", "kind": block["kind"], "title": _text(title, 300),
            "page": _page(block.get("page")), "sourceBlockOrders": [order],
            "columns": columns, "rows": rows, "markdown": body,
            "totalRowCount": total, "dataAvailable": metadata.get("chartDataAvailable") is not False and bool(columns or rows),
            "truncationReasons": reasons,
        })
    return bound_tables(candidates) if candidates else None


def bound_tables(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    remaining_rows, remaining_cells = MAX_TOTAL_ROWS, MAX_TOTAL_CELLS
    for original in candidates[:MAX_TABLES]:
        item = dict(original)
        reasons = list(item.get("truncationReasons") or [])
        raw_columns, raw_rows = item.get("columns") or [], item.get("rows") or []
        if len(raw_columns) > MAX_COLUMNS:
            reasons.append("columns")
        column_limit = min(MAX_COLUMNS, remaining_cells)
        if column_limit < min(MAX_COLUMNS, len(raw_columns)):
            reasons.append("total_cells")
        columns = [_text(cell) for cell in raw_columns[:column_limit]]
        width = len(columns)
        row_limit = min(MAX_ROWS, remaining_rows, (remaining_cells - width) // width) if width else 0
        if len(raw_rows) > row_limit:
            reasons.append("rows" if len(raw_rows) > MAX_ROWS else "total_rows_or_cells")
        rows = []
        for raw in raw_rows[:row_limit]:
            if not isinstance(raw, list):
                reasons.append("invalid_rows")
                continue
            if len(raw) > width:
                reasons.append("columns")
            row = [_text(cell) for cell in raw[:width]]
            if any(isinstance(cell, str) and len(cell) > 500 for cell in raw[:width]):
                reasons.append("cells")
            rows.append(row + [""] * max(0, width - len(row)))
        if any(isinstance(cell, str) and len(cell) > 500 for cell in raw_columns[:MAX_COLUMNS]):
            reasons.append("cells")
        markdown = _bytes(str(item.get("markdown") or ""), 16 * 1024)
        if markdown != item.get("markdown", ""):
            reasons.append("markdown")
        item.update({"kind": item.get("kind", "table"), "sourceBlockOrders": item.get("sourceBlockOrders", []),
                     "columns": columns, "rows": rows, "markdown": markdown,
                     "totalRowCount": item.get("totalRowCount", len(raw_rows)),
                     "dataAvailable": item.get("dataAvailable", bool(columns or rows))})
        item["truncationReasons"] = list(dict.fromkeys(reasons))
        item["truncated"] = bool(item["truncationReasons"])
        if _size([*result, item]) > MAX_TABLE_BYTES:
            item["truncationReasons"] = list(dict.fromkeys([*item["truncationReasons"], "bytes"]))
            item["truncated"] = True
            item["markdown"] = ""
            while rows and _size([*result, item]) > MAX_TABLE_BYTES:
                rows.pop()
            if _size([*result, item]) > MAX_TABLE_BYTES:
                break
        remaining_rows -= len(rows)
        remaining_cells -= width + sum(map(len, rows))
        result.append(item)
    if result and len(result) < len(candidates):
        result[-1]["truncated"] = True
        result[-1]["truncationReasons"] = list(dict.fromkeys([*result[-1]["truncationReasons"], "tables"]))
    # Reserve a small margin for the final omission marker above.
    while result and _size(result) > MAX_TABLE_BYTES:
        if result[-1]["rows"]:
            result[-1]["rows"].pop()
        else:
            result.pop()
    return result


def _source_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme or parts.netloc or value.startswith(("/", "\\")) or ".." in PurePosixPath(value.replace("\\", "/")).parts or "\x00" in value:
        return None
    return value


def enrich_assets(assets: list[dict[str, Any]], blocks: Any) -> list[dict[str, Any]]:
    by_hash = {asset["assetId"] for asset in assets}
    by_name: dict[str, set[str]] = {}
    for asset in assets:
        by_name.setdefault(str(asset["name"]), set()).add(asset["assetId"])
    state: dict[str, dict[str, Any]] = {digest: {"locations": [], "pages": [], "sourcePaths": [], "locationCount": 0,
                                                       "locationsTruncated": False, "allPagesKnown": True,
                                                       "firstPage": None, "samePage": True, "caption": None,
                                                       "captionIdentity": None, "sameCaption": True}
                                         for digest in by_hash}
    remaining_locations, remaining_bytes = MAX_TOTAL_LOCATIONS, MAX_LOCATION_BYTES
    for order, block, metadata in _blocks(blocks):
        explicit_hash = metadata.get("assetSha256")
        if explicit_hash:
            digest = explicit_hash if isinstance(explicit_hash, str) and explicit_hash in by_hash else None
        else:
            matches: set[str] = set()
            for key in ("imagePath", "assetName"):
                path = _source_path(metadata.get(key))
                if path:
                    matches.update(by_name.get(PurePosixPath(path).name, set()))
            digest = next(iter(matches)) if len(matches) == 1 else None
        if digest is None:
            continue
        info = state[digest]
        page = _page(block.get("page"))
        caption = metadata.get("caption")
        if not caption and metadata.get("textSource") == "embedded-alt-text":
            caption = block["text"]
        original_caption = caption
        caption_identity = original_caption if isinstance(original_caption, str) and original_caption.strip() else None
        caption = _bytes(caption, 512) if isinstance(caption, str) and caption.strip() else None
        if isinstance(original_caption, str) and caption is not None and caption != original_caption:
            info["locationsTruncated"] = True
        info["locationCount"] += 1
        info["allPagesKnown"] = info["allPagesKnown"] and page is not None
        if info["locationCount"] == 1:
            info["firstPage"], info["caption"] = page, caption
            info["captionIdentity"] = caption_identity
        else:
            info["samePage"] = info["samePage"] and page == info["firstPage"]
            info["sameCaption"] = info["sameCaption"] and caption_identity == info["captionIdentity"]
        path = _source_path(metadata.get("imagePath"))
        if isinstance(metadata.get("imagePath"), str) and len(metadata["imagePath"].encode("utf-8")) > 512:
            info["locationsTruncated"] = True
        location: dict[str, Any] = {"sourceBlockOrder": order, "page": page, "caption": caption, "kind": _text(block["kind"], 64)}
        bbox = block.get("bbox")
        if isinstance(bbox, (tuple, list)) and len(bbox) == 4:
            try:
                if all(type(value) in (int, float) and math.isfinite(value) for value in bbox):
                    location["bbox"] = list(bbox)
            except OverflowError:
                pass
        if isinstance(metadata.get("coordinateSystem"), str):
            location["coordinateSystem"] = _text(metadata["coordinateSystem"], 64)
        new_path = bool(path and path not in info["sourcePaths"])
        size = _size(location) + (_size(path) + 2 if new_path else 0)
        if len(info["locations"]) < MAX_LOCATIONS and remaining_locations > 0 and size <= remaining_bytes:
            info["locations"].append(location)
            if page is not None and page not in info["pages"]:
                info["pages"].append(page)
            if new_path:
                info["sourcePaths"].append(path)
            remaining_locations -= 1
            remaining_bytes -= size
        else:
            info["locationsTruncated"] = True
    result = []
    projection_bytes = 0
    for asset in assets:
        info = state[asset["assetId"]]
        projection = {"page": info["firstPage"] if info["allPagesKnown"] and info["samePage"] else None,
                      "pages": sorted(info["pages"]), "caption": info["caption"] if info["sameCaption"] else None,
                      "locations": list(info["locations"]), "sourcePaths": list(info["sourcePaths"]),
                      "locationCount": info["locationCount"], "locationsTruncated": info["locationsTruncated"]}
        # Leave room for bounded empty projections of the remaining assets.
        budget = max(0, MAX_LOCATION_BYTES - projection_bytes - 180 * (len(assets) - len(result) - 1))
        while _size(projection) > budget and projection["locations"]:
            projection["locations"].pop()
            projection["locationsTruncated"] = True
        if _size(projection) > budget:
            projection.update({"pages": [], "sourcePaths": [], "caption": None, "locationsTruncated": True})
        projection_bytes += _size(projection)
        result.append({**asset, **projection})
    return result
