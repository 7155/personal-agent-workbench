"""Bounded, local OOXML extraction into source-owned paragraphs and tables.

The caller validates the ZIP envelope. This layer interprets document structure;
it does not render Office layouts or infer Word pages from XML part boundaries.
"""

from __future__ import annotations

import html
import hashlib
import mimetypes
import posixpath
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any, Iterator
from xml.etree import ElementTree as ET

from .models import DocumentParseError, ParsedAsset, ParsedBlock, ParsedDocument


_MAX_COLUMN = 16_384
_MAX_ROW = 1_048_576
_MAX_CELLS = 100_000
_GRID_COLUMNS = 256
_TABLE_BATCH_ROWS = 128


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _attr(node: ET.Element, name: str, default: str = "") -> str:
    return next((value for key, value in node.attrib.items() if key.rsplit("}", 1)[-1] == name), default)


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _tag(child) == name]


def _descendant(node: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in node.iter() if _tag(child) == name), None)


def _root(archive: zipfile.ZipFile, name: str) -> ET.Element:
    raw = archive.read(name)
    declarations = raw.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise DocumentParseError("Office XML declarations are not allowed", code="unsafe_archive")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise DocumentParseError(f"Office XML is malformed: {name}", code="parse_failed") from exc


def _inline_text(node: ET.Element) -> str:
    """A formatting run is not a word/paragraph boundary."""
    pieces: list[str] = []

    def visit(current: ET.Element) -> None:
        tag = _tag(current)
        if tag in {"del", "rPh", "instrText"}:
            return
        if tag == "t":
            pieces.append(current.text or "")
        elif tag == "tab":
            pieces.append("\t")
        elif tag in {"br", "cr"}:
            pieces.append("\n")
        else:
            for child in current:
                visit(child)

    visit(node)
    return "".join(pieces).replace("\x00", "")


def _items(node: ET.Element) -> Iterator[ET.Element]:
    """Keep paragraphs/tables ordered, including those inside content controls."""
    for child in node:
        if _tag(child) in {"p", "tbl"}:
            yield child
        else:
            yield from _items(child)


def _integer(value: str, *, maximum: int, default: int = 1) -> int:
    if not value:
        return default
    if not re.fullmatch(r"[0-9]{1,10}", value) or not 1 <= int(value) <= maximum:
        raise DocumentParseError("Office coordinate or span exceeds supported bounds", code="unsafe_archive")
    return int(value)


def _cell_text(cell: ET.Element) -> str:
    paragraphs = [_inline_text(item).strip() for item in cell.iter() if _tag(item) == "p"]
    return "\n".join(paragraphs).strip() if paragraphs else _inline_text(cell).strip()


def _markdown_table(rows: list[list[str]], headers: list[str] | None = None) -> str:
    width = max((len(row) for row in rows), default=0)
    if not width:
        return ""

    def line(values: list[str]) -> str:
        return "| " + " | ".join(value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>") for value in values) + " |"

    return "\n".join([
        line(headers or [""] * width), line(["---"] * width),
        *(line(row + [""] * (width - len(row))) for row in rows),
    ])


def _table_html(cells: list[dict[str, Any]], row_numbers: list[int]) -> str:
    by_row: dict[int, list[dict[str, Any]]] = {}
    for cell in cells:
        by_row.setdefault(cell["row"], []).append(cell)
    rendered: list[str] = ["<table>"]
    for row in row_numbers:
        rendered.append(f'<tr data-row="{row}">')
        for cell in sorted(by_row.get(row, []), key=lambda item: item["column"]):
            if cell.get("continuation"):
                continue
            attrs = f' data-column="{cell["column"]}"'
            for field, attribute in (("columnSpan", "colspan"), ("rowSpan", "rowspan")):
                if cell.get(field, 1) > 1:
                    attrs += f' {attribute}="{cell[field]}"'
            rendered.append(f"<td{attrs}>{html.escape(cell['text']).replace(chr(10), '<br>')}</td>")
        rendered.append("</tr>")
    return "".join([*rendered, "</table>"])


def _office_table(node: ET.Element, *, page: int | None, heading_path: tuple[str, ...], metadata: dict[str, Any]) -> ParsedBlock | None:
    rows: list[list[str]] = []
    cells: list[dict[str, Any]] = []
    vertical: dict[int, dict[str, Any]] = {}
    slots = 0
    for row_number, row_node in enumerate(_children(node, "tr"), 1):
        before = _descendant(row_node, "gridBefore")
        skipped = _attr(before, "val") if before is not None else "0"
        column = 1 if skipped == "0" else _integer(skipped, maximum=_GRID_COLUMNS - 1) + 1
        values = [""] * (column - 1)
        active_vertical: dict[int, dict[str, Any]] = {}
        for cell_node in _children(row_node, "tc"):
            span_node = _descendant(cell_node, "gridSpan")
            span = _integer(_attr(span_node, "val") if span_node is not None else _attr(cell_node, "gridSpan"), maximum=_GRID_COLUMNS)
            if column + span - 1 > _GRID_COLUMNS:
                raise DocumentParseError("Office table exceeds built-in column limit", code="unsafe_archive")
            text = _cell_text(cell_node)
            cell: dict[str, Any] = {"row": row_number, "column": column, "text": text, "columnSpan": span, "rowSpan": 1}
            merge = _descendant(cell_node, "vMerge")
            if merge is not None and _attr(merge, "val") != "restart" and column in vertical:
                origin = vertical[column]
                origin["rowSpan"] += 1
                active_vertical[column] = origin
                cell["continuation"] = True
            elif merge is not None:
                active_vertical[column] = cell
            cells.append(cell)
            values.extend([text, *([""] * (span - 1))])
            column += span
        vertical = active_vertical
        slots += len(values)
        if slots > _MAX_CELLS:
            raise DocumentParseError("Office table exceeds built-in cell limit", code="unsafe_archive")
        rows.append(values)
    if not any(value.strip() for row in rows for value in row):
        return None
    width = max(map(len, rows))
    if width * len(rows) > _MAX_CELLS:
        raise DocumentParseError("Office table exceeds built-in grid limit", code="unsafe_archive")
    rows = [row + [""] * (width - len(row)) for row in rows]
    return ParsedBlock(
        kind="table", text=_markdown_table(rows), page=page, heading_path=heading_path,
        metadata={**metadata, "rows": rows, "cells": cells, "rowNumbers": list(range(1, len(rows) + 1)), "tableHtml": _table_html(cells, list(range(1, len(rows) + 1)))},
    )


def _word_heading_level(paragraph: ET.Element, styles: dict[str, ET.Element]) -> int | None:
    props = next(iter(_children(paragraph, "pPr")), None)
    if props is None:
        return None
    style_node = _descendant(props, "pStyle")
    style_id = _attr(style_node, "val") if style_node is not None else ""
    candidates = [props]
    seen: set[str] = set()
    current = style_id
    while current in styles and current not in seen and len(seen) < 16:
        seen.add(current)
        style = styles[current]
        candidates.append(style)
        parent = _descendant(style, "basedOn")
        current = _attr(parent, "val") if parent is not None else ""
    for candidate in candidates:
        outline = _descendant(candidate, "outlineLvl")
        if outline is not None:
            value = _attr(outline, "val")
            return int(value) + 1 if re.fullmatch(r"[0-8]", value) else None
    names = [style_id]
    for candidate in candidates[1:]:
        name = _descendant(candidate, "name")
        if name is not None:
            names.append(_attr(name, "val"))
    for name in names:
        match = re.fullmatch(r"(?:heading|标题)\s*([1-9])", name, flags=re.I)
        if match:
            return int(match.group(1))
    return None


def _docx_blocks(archive: zipfile.ZipFile, entries: set[str], assets: dict[str, ParsedAsset]) -> list[ParsedBlock]:
    styles: dict[str, ET.Element] = {}
    if "word/styles.xml" in entries:
        styles = {_attr(node, "styleId"): node for node in _root(archive, "word/styles.xml") if _tag(node) == "style"}
    names = ["word/document.xml"] if "word/document.xml" in entries else []
    names.extend(sorted(name for name in entries if re.fullmatch(r"word/(header\d*|footer\d*|footnotes|endnotes)\.xml", name)))
    blocks: list[ParsedBlock] = []
    for name in names:
        headings: dict[int, str] = {}
        for item in _items(_root(archive, name)):
            path = tuple(headings[key] for key in sorted(headings))
            metadata: dict[str, Any] = {"sourcePart": name}
            if _tag(item) == "tbl":
                table = _office_table(item, page=None, heading_path=path, metadata=metadata)
                if table:
                    blocks.append(table)
                blocks.extend(_embedded_blocks(archive, entries, name, item, assets, page=None, heading_path=path))
                continue
            text = _inline_text(item).strip()
            if not text:
                blocks.extend(_embedded_blocks(archive, entries, name, item, assets, page=None, heading_path=path))
                continue
            level = _word_heading_level(item, styles)
            if level is not None:
                headings = {key: value for key, value in headings.items() if key < level}
                headings[level] = text
                path = tuple(headings[key] for key in sorted(headings))
                metadata["headingLevel"] = level
            blocks.append(ParsedBlock(kind="heading" if level else "paragraph", text=text, heading_path=path, metadata=metadata))
            blocks.extend(_embedded_blocks(archive, entries, name, item, assets, page=None, heading_path=path))
    return blocks


def _relationships(archive: zipfile.ZipFile, entries: set[str], owner: str) -> dict[str, tuple[str, str]]:
    path = PurePosixPath(owner)
    rels = str(path.parent / "_rels" / f"{path.name}.rels")
    if rels not in entries:
        return {}
    result: dict[str, tuple[str, str]] = {}
    for relation in _root(archive, rels):
        if _attr(relation, "TargetMode").lower() == "external":
            continue
        target = _attr(relation, "Target").replace("\\", "/")
        resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(str(path.parent), target))
        if resolved.startswith("../") or ":" in resolved:
            raise DocumentParseError("Office relationship escapes its package", code="unsafe_archive")
        result[_attr(relation, "Id")] = (resolved, _attr(relation, "Type"))
    return result


def _chart_block(archive: zipfile.ZipFile, source: str, *, page: int | None, heading_path: tuple[str, ...], owner: str) -> ParsedBlock:
    root = _root(archive, source)
    title_node = _descendant(root, "title")
    title = _inline_text(title_node).strip() if title_node is not None else ""
    rows: list[list[str]] = [["Series", "Category/X", "Value/Y"]]
    for number, series in enumerate((n for n in root.iter() if _tag(n) == "ser"), 1):
        tx = _descendant(series, "tx")
        label = next((n.text or "" for n in tx.iter() if _tag(n) == "v"), "") if tx is not None else ""

        def points(tags: set[str], part_owner: ET.Element = series) -> dict[int, str]:
            part = next((n for n in part_owner if _tag(n) in tags), None)
            result: dict[int, str] = {}
            if part is not None:
                for node in part.iter():
                    if _tag(node) != "pt":
                        continue
                    raw_index = node.get("idx", "")
                    if not re.fullmatch(r"\d{1,6}", raw_index) or int(raw_index) >= _MAX_CELLS:
                        raise DocumentParseError("Office chart point index exceeds bounds", code="unsafe_archive")
                    value = _descendant(node, "v")
                    result[int(raw_index)] = (value.text or "") if value is not None else ""
            return result

        categories, values = points({"cat", "xVal"}), points({"val", "yVal"})
        for index in sorted(categories.keys() | values.keys()):
            if len(rows) >= _MAX_CELLS:
                raise DocumentParseError("Office chart exceeds cached point limit", code="unsafe_archive")
            rows.append([label or f"Series {number}", categories.get(index, ""), values.get(index, "")])
    has_data = len(rows) > 1
    table_html = _table_html([{"row": index, "column": column, "text": value}
                              for index, row in enumerate(rows, 1) for column, value in enumerate(row, 1)], list(range(1, len(rows) + 1))) if has_data else ""
    text = (title + "\n" if title else "") + (table_html if has_data else "[Embedded chart; cached data unavailable]")
    return ParsedBlock("chart", text, page=page, heading_path=heading_path,
                       metadata={"sourcePart": source, "ownerPart": owner, "textSource": "chart-cached-data",
                                 "chartDataAvailable": any(row[2] for row in rows[1:]), "ocrApplied": False,
                                 **({"rows": rows, "tableHtml": table_html, "caption": title} if has_data else {})})


def _embedded_blocks(
    archive: zipfile.ZipFile, entries: set[str], source: str, node: ET.Element,
    assets: dict[str, ParsedAsset], *, page: int | None, heading_path: tuple[str, ...],
    seen: frozenset[str] = frozenset(),
) -> list[ParsedBlock]:
    if not any(_tag(ref) in {"blip", "imagedata", "chart", "drawing"}
               and (_attr(ref, "embed") or _attr(ref, "id") or _attr(ref, "link")) for ref in node.iter()):
        return []
    relations = _relationships(archive, entries, source)
    parents = {child: parent for parent in node.iter() for child in parent}
    blocks: list[ParsedBlock] = []
    for reference in node.iter():
        tag = _tag(reference)
        if tag not in {"blip", "imagedata", "chart", "drawing"}:
            continue
        identifier = _attr(reference, "embed") or _attr(reference, "id") or _attr(reference, "link")
        if not identifier:
            continue
        relation = relations.get(identifier)
        if relation is None:
            # External images/charts are deliberately not downloaded.
            blocks.append(ParsedBlock("image" if tag != "chart" else "figure", "[External or unresolved embedded media]",
                                      page=page, heading_path=heading_path,
                                      metadata={"sourcePart": source, "relationshipId": identifier, "mediaUnavailable": True, "ocrApplied": False}))
            continue
        target, relationship_type = relation
        if target not in entries:
            raise DocumentParseError(f"Office embedded media part is missing: {target}", code="parse_failed")
        if relationship_type.endswith("/drawing"):
            if target in seen or len(seen) >= 4:
                raise DocumentParseError("Office drawing relationships are cyclic or too deep", code="unsafe_archive")
            blocks.extend(_embedded_blocks(archive, entries, target, _root(archive, target), assets,
                                           page=page, heading_path=heading_path, seen=seen | {source, target}))
        elif relationship_type.endswith("/chart") or tag == "chart":
            blocks.append(_chart_block(archive, target, page=page, heading_path=heading_path, owner=source))
        elif relationship_type.endswith("/image") or tag in {"blip", "imagedata"}:
            asset = assets.get(target)
            if asset is None:
                data = archive.read(target)
                asset = ParsedAsset(PurePosixPath(target).name, mimetypes.guess_type(target)[0] or "application/octet-stream", hashlib.sha256(data).hexdigest(), data)
                assets[target] = asset
            container = reference
            while container in parents and _tag(container) not in {"drawing", "pic", "pict", "graphicFrame"}:
                container = parents[container]
            description = next((n for n in container.iter() if _tag(n) in {"docPr", "cNvPr"}), None)
            alt = (_attr(description, "descr") or _attr(description, "title")) if description is not None else ""
            blocks.append(ParsedBlock("image", alt or f"[Embedded image: {asset.name}; no OCR text]", page=page, heading_path=heading_path,
                                      metadata={"sourcePart": source, "imagePath": target, "assetName": asset.name,
                                                "assetSha256": asset.sha256, "textSource": "embedded-alt-text" if alt else "asset-placeholder", "ocrApplied": False}))
    return blocks


def _part_order(archive: zipfile.ZipFile, entries: set[str], *, owner: str, tag: str, fallback: str) -> list[tuple[str, str]]:
    if owner in entries:
        relations = _relationships(archive, entries, owner)
        ordered: list[tuple[str, str]] = []
        for node in _root(archive, owner).iter():
            if _tag(node) != tag:
                continue
            # Presentation has both numeric id and r:id; only the namespaced
            # relationship ID identifies the slide part.
            relationship_id = next((value for key, value in node.attrib.items() if key.endswith("}id")), _attr(node, "id"))
            relation = relations.get(relationship_id)
            if relation is None or relation[0] not in entries:
                raise DocumentParseError("Office manifest references a missing document part", code="parse_failed")
            ordered.append((relation[0], _attr(node, "name", PurePosixPath(relation[0]).stem)))
        if ordered:
            return ordered
    names = sorted((name for name in entries if re.fullmatch(fallback, name)), key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)))
    return [(name, PurePosixPath(name).stem) for name in names]


def _slide_blocks(root: ET.Element, *, page: int, source: str, archive: zipfile.ZipFile, entries: set[str], assets: dict[str, ParsedAsset], notes: bool = False, heading_path: tuple[str, ...] = ()) -> list[ParsedBlock]:
    tree = _descendant(root, "spTree")
    def shapes(node: ET.Element) -> Iterator[ET.Element]:
        for child in node:
            if _tag(child) == "grpSp":
                yield from shapes(child)
            else:
                yield child

    containers = list(shapes(tree)) if tree is not None else [root]
    blocks: list[ParsedBlock] = []
    for container in containers:
        placeholder = _descendant(container, "ph")
        kind = _attr(placeholder, "type") if placeholder is not None else ""
        if notes and kind in {"sldNum", "sldImg", "dt", "hdr", "ftr"}:
            continue
        items = list(_items(container))
        if not items and _inline_text(container).strip():
            blocks.append(ParsedBlock(kind="speaker_notes" if notes else "paragraph", text=_inline_text(container).strip(), page=page, heading_path=heading_path, metadata={"sourcePart": source, "slideNumber": page}))
        for item in items:
            metadata: dict[str, Any] = {"sourcePart": source, "slideNumber": page}
            if _tag(item) == "tbl":
                table = _office_table(item, page=page, heading_path=heading_path, metadata=metadata)
                if table:
                    blocks.append(table)
                continue
            text = _inline_text(item).strip()
            if not text:
                continue
            heading = kind in {"title", "ctrTitle"} and not notes
            if heading:
                heading_path = (text,)
                metadata["headingLevel"] = 1
            blocks.append(ParsedBlock(kind="heading" if heading else "speaker_notes" if notes else "paragraph", text=text, page=page, heading_path=heading_path, metadata=metadata))
        blocks.extend(_embedded_blocks(archive, entries, source, container, assets, page=page, heading_path=heading_path))
    return blocks


def _pptx_blocks(archive: zipfile.ZipFile, entries: set[str], assets: dict[str, ParsedAsset]) -> tuple[list[ParsedBlock], int]:
    slides = _part_order(archive, entries, owner="ppt/presentation.xml", tag="sldId", fallback=r"ppt/slides/slide\d+\.xml")
    blocks: list[ParsedBlock] = []
    for page, (name, _) in enumerate(slides, 1):
        slide = _slide_blocks(_root(archive, name), page=page, source=name, archive=archive, entries=entries, assets=assets)
        blocks.extend(slide)
        heading = next((block.heading_path for block in slide if block.kind == "heading"), ())
        for target, kind in _relationships(archive, entries, name).values():
            if kind.endswith("/notesSlide") and target in entries:
                blocks.extend(_slide_blocks(_root(archive, target), page=page, source=target, archive=archive, entries=entries, assets=assets, notes=True, heading_path=heading))
    return blocks, len(slides)


def _coordinate(reference: str) -> tuple[int, int]:
    match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})", reference)
    if not match:
        raise DocumentParseError("XLSX contains an invalid cell coordinate", code="unsafe_archive")
    column = 0
    for character in match.group(1).upper():
        column = column * 26 + ord(character) - ord("A") + 1
    row = _integer(match.group(2), maximum=_MAX_ROW)
    if column > _MAX_COLUMN:
        raise DocumentParseError("XLSX cell column exceeds format bounds", code="unsafe_archive")
    return row, column


def _column_name(column: int) -> str:
    value = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _sheet_tables(root: ET.Element, shared: list[str], *, source: str, name: str, budget: list[int]) -> list[ParsedBlock]:
    sheet_rows: list[tuple[int, list[dict[str, Any]]]] = []
    previous_row = 0
    max_column = 0
    for row_node in root.iter():
        if _tag(row_node) != "row":
            continue
        row_number = _integer(_attr(row_node, "r"), maximum=_MAX_ROW, default=previous_row + 1)
        previous_row = row_number
        cells: list[dict[str, Any]] = []
        previous_column = 0
        for cell in _children(row_node, "c"):
            budget[0] += 1
            if budget[0] > _MAX_CELLS:
                raise DocumentParseError("XLSX exceeds built-in 100000-cell limit", code="unsafe_archive")
            reference = _attr(cell, "r")
            source_row, column = _coordinate(reference) if reference else (row_number, previous_column + 1)
            if source_row != row_number or column > _MAX_COLUMN:
                raise DocumentParseError("XLSX row and cell coordinates disagree", code="parse_failed")
            previous_column = column
            value_node = _descendant(cell, "v")
            value = str(value_node.text or "") if value_node is not None else ""
            cell_type = _attr(cell, "t")
            if cell_type == "s":
                if not value.isdigit() or len(value) > 9 or int(value) >= len(shared):
                    raise DocumentParseError("XLSX shared string reference is invalid", code="parse_failed")
                value = shared[int(value)]
            elif cell_type == "inlineStr":
                value = _inline_text(cell)
            formula = _descendant(cell, "f")
            formula_text = (formula.text or "") if formula is not None else ""
            metadata: dict[str, Any] = {"row": row_number, "column": column, "reference": reference or f"{_column_name(column)}{row_number}", "text": value if value or not formula_text else f"={formula_text}"}
            if formula is not None:
                metadata["formula"] = formula_text
                metadata["cachedValueAvailable"] = value_node is not None
            cells.append(metadata)
            max_column = max(max_column, column)
        if cells:
            sheet_rows.append((row_number, sorted(cells, key=lambda item: item["column"])))
    merges: list[str] = []
    for node in root.iter():
        if _tag(node) == "mergeCell":
            reference = _attr(node, "ref")
            parts = reference.split(":")
            if len(parts) != 2:
                raise DocumentParseError("XLSX merged cell range is invalid", code="parse_failed")
            start, end = map(_coordinate, parts)
            if start[0] > end[0] or start[1] > end[1]:
                raise DocumentParseError("XLSX merged cell range is reversed", code="parse_failed")
            merges.append(reference)
            max_column = max(max_column, end[1])
    blocks: list[ParsedBlock] = []
    for offset in range(0, len(sheet_rows), _TABLE_BATCH_ROWS):
        batch = sheet_rows[offset:offset + _TABLE_BATCH_ROWS]
        actual_cells = [cell for _, cells in batch for cell in cells]
        if not any(cell["text"].strip() for cell in actual_cells):
            continue
        # Bounding each batch alone still permits enormous padding across many
        # rows. Keep rendered size proportional to actual cells as well.
        sparse = max_column > _GRID_COLUMNS or max_column * len(batch) > max(64, len(actual_cells) * 8)
        row_numbers = [row for row, _ in batch]
        if sparse:
            rows = [[cell["reference"], cell["text"]] for cell in actual_cells]
            headers = ["Cell", "Value"]
            html_cells = [{"row": index, "column": column, "text": value} for index, values in enumerate(rows, 1) for column, value in enumerate(values, 1)]
            rendered_row_numbers = list(range(1, len(rows) + 1))
        else:
            rows = []
            for _, cells in batch:
                values = [""] * max_column
                for cell in cells:
                    values[cell["column"] - 1] = cell["text"]
                rows.append(values)
            headers = [_column_name(column) for column in range(1, max_column + 1)]
            html_cells = [{"row": row_number, "column": column, "text": value} for row_number, values in zip(row_numbers, rows) for column, value in enumerate(values, 1)]
            rendered_row_numbers = row_numbers
        blocks.append(ParsedBlock(kind="table", text=_markdown_table(rows, headers), heading_path=(name,), metadata={
            "sourcePart": source, "sheetName": name, "representation": "coordinate-values" if sparse else "grid",
            "rows": rows, "rowNumbers": row_numbers, "rowStart": min(row_numbers), "rowEnd": max(row_numbers),
            "columnStart": 1, "columnEnd": max_column, "cells": actual_cells, "mergeRanges": merges,
            "tableHtml": _table_html(html_cells, rendered_row_numbers),
        }))
    return blocks


def _xlsx_blocks(archive: zipfile.ZipFile, entries: set[str], assets: dict[str, ParsedAsset]) -> list[ParsedBlock]:
    shared = [_inline_text(node) for node in _root(archive, "xl/sharedStrings.xml") if _tag(node) == "si"] if "xl/sharedStrings.xml" in entries else []
    sheets = _part_order(archive, entries, owner="xl/workbook.xml", tag="sheet", fallback=r"xl/worksheets/sheet\d+\.xml")
    blocks: list[ParsedBlock] = []
    budget = [0]
    for source, name in sheets:
        tables = _sheet_tables(_root(archive, source), shared, source=source, name=name, budget=budget)
        if tables:
            blocks.append(ParsedBlock(kind="heading", text=name, heading_path=(name,), metadata={"sourcePart": source, "sheetName": name, "headingLevel": 1}))
            blocks.extend(tables)
        blocks.extend(_embedded_blocks(archive, entries, source, _root(archive, source), assets, page=None, heading_path=(name,)))
    return blocks


def parse_office_archive(archive: zipfile.ZipFile, entries: set[str], *, suffix: str, title: str) -> ParsedDocument:
    metadata: dict[str, Any] = {"structureVersion": "office-blocks-v1"}
    assets: dict[str, ParsedAsset] = {}
    if suffix == ".docx":
        blocks = _docx_blocks(archive, entries, assets)
    elif suffix == ".pptx":
        blocks, pages = _pptx_blocks(archive, entries, assets)
        metadata.update({"pageCount": pages, "pageSeparator": "\f", "pageUnit": "slide"})
    else:
        blocks = _xlsx_blocks(archive, entries, assets)
    bodies: dict[int, list[str]] = {}
    for block in blocks:
        text = block.text
        if block.kind == "heading":
            text = "#" * min(6, block.metadata.get("headingLevel", 1)) + " " + text
        bodies.setdefault(block.page or 1, []).append(text)
    text = "\f".join("\n\n".join(bodies.get(page, [])) for page in range(1, metadata.get("pageCount", 1) + 1))
    if not text.strip():
        raise DocumentParseError("Office document contains no readable text", code="empty_document")
    metadata.update({"embeddedAssetCount": len(assets), "ocrApplied": False})
    return ParsedDocument(text=text, title=title, provider="builtin", provider_version=f"{suffix[1:]}-xml-v3", assets=tuple(assets.values()), metadata=metadata, blocks=tuple(blocks))
