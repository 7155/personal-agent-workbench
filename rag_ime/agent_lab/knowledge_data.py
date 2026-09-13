"""Bounded Lab intake; normalized corpus and evaluation references stay separate.

This module supplies data to the existing Knowledge sandbox and evaluators. It
does not implement chunking, embeddings, retrieval, metrics or an Agent loop.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

MAX_BYTES = 300 * 1024 * 1024
MAX_DOCUMENTS = 20_000
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_CASES = 10_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".html", ".htm"}
_IGNORED = {"node_modules", "__pycache__", ".git", ".venv", "venv", "dist", "build"}


class KnowledgeIntakeError(ValueError):
    """User-facing validation text, without source contents or credentials."""


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    hashed = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as out:
        os.chmod(path, 0o600)
        out.write(canonical(value) + "\n")


def read_jsonl(path: Path, *, maximum: int = MAX_DOCUMENTS) -> list[dict]:
    rows = []
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise KnowledgeIntakeError("文件不存在或超过 300 MB，请检查所选来源。")
    try:
        with path.open(encoding="utf-8-sig") as source:
            for number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                if len(line.encode("utf-8")) > MAX_DOCUMENT_BYTES * 2:
                    raise KnowledgeIntakeError(f"第 {number} 行过大，请按独立文档拆分。")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise KnowledgeIntakeError(f"第 {number} 行必须是 JSON 对象。")
                rows.append(row)
                if len(rows) > maximum:
                    raise KnowledgeIntakeError(f"记录超过本次上限 {maximum:,} 条。")
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise KnowledgeIntakeError("文件不是有效的 UTF-8 JSONL，请检查编码与每行 JSON。") from exc
    if not rows:
        raise KnowledgeIntakeError("文件中没有可读取的记录。")
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as out:
        os.chmod(path, 0o600)
        for row in rows:
            out.write(canonical(dict(row)) + "\n")
    return file_hash(path)


def _text(value: object, label: str, *, maximum: int = 100_000) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > maximum:
        raise KnowledgeIntakeError(f"{label}需要非空文本，且不能超过 {maximum:,} 个字符。")
    return value


def normalize_documents(rows: list[dict], fields: Mapping | None = None) -> list[dict]:
    fields = dict(fields or {})
    if set(fields) - {"id", "title", "text", "uri"} or any(not isinstance(value, str) or not value for value in fields.values()):
        raise KnowledgeIntakeError("文档字段映射仅支持 id、title、text 和 uri。")
    documents, seen, total = [], set(), 0
    for number, row in enumerate(rows, 1):
        # A QA file must never accidentally become the answerer's corpus.
        text = row.get(fields["text"]) if "text" in fields else next((row[key] for key in ("contents", "text", "content") if key in row), None)
        source_id = row.get(fields.get("id", "id"), row.get("sourceId"))
        source_id = _text(str(source_id) if isinstance(source_id, int) and not isinstance(source_id, bool) else source_id,
                          f"第 {number} 条文档的来源 ID", maximum=1000)
        text = _text(text, f"第 {number} 条文档的正文", maximum=MAX_DOCUMENT_BYTES)
        size = len(text.encode("utf-8"))
        total += size
        if size > MAX_DOCUMENT_BYTES or total > MAX_BYTES:
            raise KnowledgeIntakeError("正文超过 4 MB 单篇或 300 MB 总量，请拆分知识库。")
        if source_id in seen:
            raise KnowledgeIntakeError(f"第 {number} 条文档的来源 ID 重复；请保留独立版本标识。")
        seen.add(source_id)
        external_id = source_id if _ID.fullmatch(source_id) else "doc:" + digest(source_id)[:40]
        title = row.get(fields.get("title", "title")) or row.get("name") or source_id
        uri = row.get(fields.get("uri", "url")) or row.get("uri") or ""
        if not isinstance(title, str) or not isinstance(uri, str) or len(uri) > 4000:
            raise KnowledgeIntakeError(f"第 {number} 条文档的标题或来源地址无效。")
        documents.append({"sourceId": source_id, "externalId": external_id, "title": title[:240], "uri": uri,
                          "text": text, "byteSize": size, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    if not documents or len(documents) > MAX_DOCUMENTS:
        raise KnowledgeIntakeError(f"知识库需要 1–{MAX_DOCUMENTS:,} 篇独立文档。")
    if len({row["externalId"] for row in documents}) != len(documents):
        raise KnowledgeIntakeError("来源标识映射发生冲突，请调整来源 ID。")
    return documents


def content_identities(documents: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """One index document per exact body; retain every original source identity.

    The first source in the immutable corpus is canonical. This matches the
    Knowledge owner's byte-hash deduplication without changing source text.
    """
    unique, by_hash, aliases = [], {}, {}
    for document in documents:
        content_hash = hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
        if content_hash not in by_hash:
            by_hash[content_hash] = document["sourceId"]
            unique.append(document)
        aliases[document["sourceId"]] = by_hash[content_hash]
    return unique, aliases


def collect_folder(path: Path, *, progress: Callable[[str], None], cancelled: Callable[[], bool]) -> tuple[list[dict], dict]:
    from ..knowledge_library import KnowledgeLibraryConfig
    from ..knowledge_library.parsers import ParserRouter

    root = path.resolve(strict=True)
    if not root.is_dir():
        raise KnowledgeIntakeError("请选择文档文件夹，或选择 JSONL 语料文件。")
    rows, skipped, scanned, total = [], 0, 0, 0
    parser = ParserRouter(KnowledgeLibraryConfig(root))
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if not name.startswith(".") and name not in _IGNORED
                          and not (Path(directory) / name).is_symlink())
        for name in sorted(files):
            if cancelled():
                raise InterruptedError("已停止资料整理。")
            scanned += 1
            if scanned > 50_000:
                raise KnowledgeIntakeError("文件夹条目超过 50,000，请选择更具体的资料文件夹。")
            source = Path(directory) / name
            if name.startswith(".") or source.is_symlink() or source.suffix.lower() not in _TEXT_SUFFIXES:
                skipped += 1
                continue
            size = source.stat().st_size
            total += size
            if size > MAX_DOCUMENT_BYTES or total > MAX_BYTES:
                raise KnowledgeIntakeError("文件夹超过 4 MB 单篇或 300 MB 总量；请拆分后接入。")
            try:
                parsed = parser.parse(source, mode="builtin")
            except Exception as exc:
                raise KnowledgeIntakeError(f"无法解析文件 {name[:120]}，请检查编码和格式。") from exc
            relative = source.relative_to(root).as_posix()
            rows.append({"id": relative, "title": parsed.title or relative, "text": parsed.text, "uri": source.as_uri()})
            if len(rows) > MAX_DOCUMENTS:
                raise KnowledgeIntakeError("文档超过 20,000 篇，请拆分知识库。")
            if len(rows) % 50 == 0:
                progress(f"已整理 {len(rows):,} 篇文档")
    return normalize_documents(rows), {"skippedCount": skipped, "scannedCount": scanned, "sourceBytes": total,
                                       "supportedFormats": sorted(_TEXT_SUFFIXES)}


def normalize_cases(rows: list[dict], documents: list[dict], fields: Mapping | None = None) -> tuple[list[dict], dict]:
    fields = dict(fields or {})
    if set(fields) - {"id", "question", "answer", "sources", "split"} or any(not isinstance(value, str) or not value for value in fields.values()):
        raise KnowledgeIntakeError("评测集字段映射仅支持 id、question、answer、sources 和 split。")
    known = {row["sourceId"] for row in documents}
    _, source_aliases = content_identities(documents)
    result, seen = [], set()
    for number, row in enumerate(rows, 1):
        question = _text(row.get(fields.get("question", "question")), f"第 {number} 题的问题", maximum=20_000)
        answer = row.get(fields.get("answer", "answer"), "")
        if not isinstance(answer, str) or len(answer) > 100_000:
            raise KnowledgeIntakeError(f"第 {number} 题的参考答案无效。")
        ids = row.get(fields.get("sources", "article_ids"), row.get("sourceIds", []))
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except json.JSONDecodeError:
                ids = [value.strip() for value in ids.split(";") if value.strip()]
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(value, str) or value not in known for value in ids):
            raise KnowledgeIntakeError(f"第 {number} 题的参考来源不存在；请核对来源 ID 和知识库版本。")
        raw_id = row.get(fields.get("id", "id"), row.get("task_id"))
        case_id = str(raw_id) if raw_id is not None else "case:" + digest([question, answer, sorted(ids)])[:32]
        _text(case_id, "题目 ID", maximum=240)
        if case_id in seen:
            raise KnowledgeIntakeError(f"第 {number} 题的 ID 重复，请先去重。")
        seen.add(case_id)
        source_split = row.get(fields.get('split', 'split'))
        if source_split not in (None, '', 'development', 'holdout'):
            raise KnowledgeIntakeError(f"第 {number} 题的分组需要是 development 或 holdout。")
        result.append({"caseId": case_id, "question": question, "answer": answer, "sourceIds": list(dict.fromkeys(ids)),
                       "sourceRow": number, "provenance": "imported_reference", "retrievalEvaluable": bool(ids),
                       **({'sourceSplit': source_split} if source_split else {})})
    if not result or len(result) > MAX_CASES:
        raise KnowledgeIntakeError(f"评测集需要 1–{MAX_CASES:,} 条问题。")
    # Connected components prevent exact duplicate questions OR shared gold
    # documents from crossing this locally derived development/holdout split.
    parents, first = list(range(len(result))), {}
    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    for index, row in enumerate(result):
        keys = ["q:" + " ".join(row["question"].casefold().split())] + ["d:" + source_aliases[value] for value in row["sourceIds"]]
        for key in keys:
            if key in first:
                parents[find(index)] = find(first[key])
            else:
                first[key] = index
    groups: dict[int, list[int]] = {}
    for index in range(len(result)):
        groups.setdefault(find(index), []).append(index)
    for members in groups.values():
        family = digest(sorted(result[index]["caseId"] for index in members))
        supplied = {result[index]['sourceSplit'] for index in members if result[index].get('sourceSplit')}
        if len(supplied) > 1:
            raise KnowledgeIntakeError("原评测分组存在交叉：相同问题或共享参考来源同时出现在开发集和保留集。请先核对分组，系统不会静默重分。")
        split = next(iter(supplied)) if supplied else (
            "development" if int(digest(["paw-knowledge-split-v1", family])[:8], 16) % 100 < 70 else "holdout")
        for index in members:
            result[index].update(familyId=family, split=split)
    counts = {split: sum(row["split"] == split for row in result) for split in ("development", "holdout")}
    return result, {"caseCount": len(result), "splits": counts, "familyCount": len(groups),
                    "retrievalEvaluableCount": sum(row["retrievalEvaluable"] for row in result),
                    "referenceAnswerCount": sum(bool(row["answer"].strip()) for row in result),
                    "preview": [{"caseId": row["caseId"], "question": row["question"]} for row in result if row["split"] == "development"][:3],
                    "splitPolicy": "provided splits preserved; shared exact-content sources and exact normalized questions stay together"
                        if any(row.get('sourceSplit') for row in result) else "paw-knowledge-split-v2; shared exact-content sources and exact normalized questions stay together",
                    "providedSplit": all(row.get('sourceSplit') for row in result),
                    "officialSplit": False}


def read_case_file(path: Path) -> list[dict]:
    if path.suffix.lower() != ".csv":
        return read_jsonl(path, maximum=MAX_CASES)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise KnowledgeIntakeError("CSV 评测集超过 20 MB，请拆分后导入。")
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            rows = []
            for row in csv.DictReader(source):
                rows.append(dict(row))
                if len(rows) > MAX_CASES:
                    raise KnowledgeIntakeError("CSV 评测集超过 10,000 条问题。")
            return rows
    except UnicodeError as exc:
        raise KnowledgeIntakeError("CSV 评测集需要 UTF-8 编码。") from exc
