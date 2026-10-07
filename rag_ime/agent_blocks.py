from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Mapping, Sequence


BLOCK_SCHEMA_VERSION = "rag-ime.agent-block.v1"
ENVELOPE_SCHEMA_VERSION = "rag-ime.agent-blocks.v1"
MAX_BLOCKS_PER_TURN = 16
MAX_BLOCK_BYTES = 64 * 1024
MAX_TURN_BLOCK_BYTES = 256 * 1024
MAX_SUMMARY_CHARS = 240

RICH_BLOCK_TYPES = frozenset(
    {"card", "checklist", "table", "artifact", "reference", "status"}
)
KNOWN_BLOCK_TYPES = frozenset(
    {
        "text", "code", "reasoning_summary", "progress", "tool_call",
        "tool_result", "citation", "image", "audio", "file", "sticker",
        "task_plan", "diff", "approval", "error", *RICH_BLOCK_TYPES,
    }
)
_FENCE = re.compile(r"```rag_ime_blocks[ \t]*\r?\n([\s\S]*?)\r?\n```")
_FORBIDDEN_KEYS = frozenset({"html", "script", "style", "srcdoc", "javascript", "css"})
_SAFE_URL = re.compile(r"^(?:https://|/api/agent/media/|media:|artifact:)", re.IGNORECASE)


@dataclass(frozen=True)
class BlockExtraction:
    text: str
    blocks: tuple[dict[str, object], ...]
    before_bytes: int
    after_bytes: int
    preserved_invalid_fences: int

    def receipt(self) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.agent-block-cleaner-receipt.v1",
            "beforeBytes": self.before_bytes,
            "afterBytes": self.after_bytes,
            "estimatedTokensBefore": (self.before_bytes + 3) // 4,
            "estimatedTokensAfter": (self.after_bytes + 3) // 4,
            "cleanedBlockCount": len(self.blocks),
            "preservedInvalidFenceCount": self.preserved_invalid_fences,
        }


def normalize_trusted_agent_blocks(
    values: object,
    *,
    source_kind: str,
    source_ref: str,
    visibility: str = "private_session",
    generation: int = 0,
) -> tuple[dict[str, object], ...]:
    """Server tool/event main path; payload cannot choose its own scope fields."""

    if values is None:
        return ()
    if not isinstance(values, list) or len(values) > MAX_BLOCKS_PER_TURN:
        raise ValueError("Trusted Agent blocks must be a bounded array")
    normalized = [
        normalize_agent_block_payload(
            value,
            source_kind=source_kind,
            source_ref=source_ref,
            visibility=visibility,
            generation=generation,
        )
        for value in values
    ]
    if any(value is None for value in normalized):
        raise ValueError("Trusted Agent block payload failed validation")
    result = tuple(value for value in normalized if value is not None)
    if sum(len(_canonical_json(value).encode("utf-8")) for value in result) > MAX_TURN_BLOCK_BYTES:
        raise ValueError("Trusted Agent blocks exceed turn byte budget")
    return result


def extract_completed_agent_blocks(
    text: str,
    *,
    source_kind: str,
    source_ref: str,
    visibility: str = "private_session",
    generation: int = 0,
) -> BlockExtraction:
    """Extract only complete, wholly valid fences; invalid content stays readable."""

    blocks: list[dict[str, object]] = []
    invalid = 0
    consumed_bytes = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal invalid, consumed_bytes
        raw_json = match.group(1)
        if len(raw_json.encode("utf-8")) > MAX_TURN_BLOCK_BYTES:
            invalid += 1
            return match.group(0)
        try:
            envelope = json.loads(raw_json)
        except (TypeError, ValueError):
            invalid += 1
            return match.group(0)
        raw_blocks = envelope.get("blocks") if isinstance(envelope, Mapping) else None
        if (
            not isinstance(envelope, Mapping)
            or envelope.get("schemaVersion") != ENVELOPE_SCHEMA_VERSION
            or not isinstance(raw_blocks, list)
            or not 0 < len(raw_blocks) <= MAX_BLOCKS_PER_TURN
        ):
            invalid += 1
            return match.group(0)
        normalized = [
            normalize_agent_block_payload(
                value,
                source_kind=source_kind,
                source_ref=source_ref,
                visibility=visibility,
                generation=generation,
            )
            for value in raw_blocks
        ]
        if any(value is None for value in normalized):
            invalid += 1
            return match.group(0)
        canonical = [value for value in normalized if value is not None]
        size = sum(len(_canonical_json(value).encode("utf-8")) for value in canonical)
        if consumed_bytes + size > MAX_TURN_BLOCK_BYTES:
            invalid += 1
            return match.group(0)
        consumed_bytes += size
        blocks.extend(canonical)
        return ""

    clean_text = _FENCE.sub(replace, text).strip()
    return BlockExtraction(
        text=clean_text,
        blocks=tuple(blocks),
        before_bytes=len(text.encode("utf-8")),
        after_bytes=len(provider_block_projection(clean_text, blocks).encode("utf-8")),
        preserved_invalid_fences=invalid,
    )


def normalize_agent_block_payload(
    value: object,
    *,
    source_kind: str,
    source_ref: str,
    visibility: str,
    generation: int,
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    block_id = _compact(value.get("id"), 160)
    original_type = _compact(value.get("type"), 80)
    if not block_id or not original_type or original_type == "html_widget":
        return None
    raw_data = value.get("data")
    data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
    if not _safe_data(data):
        return None
    if len(_canonical_json(value).encode("utf-8")) > MAX_BLOCK_BYTES:
        return None
    block_type = original_type if original_type in KNOWN_BLOCK_TYPES else "unknown"
    if block_type == "unknown":
        data = {"originalType": original_type}
    summary = _summary(block_type, data)
    if not summary:
        return None
    digest = _content_digest(block_type, data)
    presentation = _presentation(block_type)
    block_ref = _bound_ref(source_ref, block_id, max(0, int(generation)), digest)
    return {
        "schemaVersion": BLOCK_SCHEMA_VERSION,
        "id": block_id,
        "type": block_type,
        "status": "completed",
        "presentationKind": presentation,
        "data": data,
        "summary": summary,
        "source": {"kind": _compact(source_kind, 80), "ref": _compact(source_ref, 240)},
        "visibility": visibility if visibility in {"private_session", "room_post", "root_post"} else "private_session",
        "digest": digest,
        "ref": block_ref,
        "generation": max(0, int(generation)),
    }


def bind_block_scope(
    values: Sequence[Mapping[str, object]],
    *,
    session_id: str,
    message_id: str,
    generation: int,
) -> list[dict[str, object]]:
    """Bind server-owned Session/message/generation identity before persistence/SSE."""

    result: list[dict[str, object]] = []
    scope = f"{session_id}:{message_id}"
    bound_generation = max(0, int(generation))
    for value in values:
        block = dict(value)
        if block.get("schemaVersion") != BLOCK_SCHEMA_VERSION:
            result.append(block)
            continue
        block_id = _compact(block.get("id"), 160)
        digest = _compact(block.get("digest"), 64)
        if not block_id or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Cannot bind invalid Agent block identity")
        if digest != _content_digest(str(block.get("type") or ""), block.get("data")):
            raise ValueError("Cannot bind Agent block with mismatched content digest")
        if block.get("summary") != _summary(str(block.get("type") or ""), dict(block.get("data") or {})):
            raise ValueError("Cannot bind Agent block with mismatched deterministic summary")
        if block.get("presentationKind") != _presentation(str(block.get("type") or "")):
            raise ValueError("Cannot bind Agent block with mismatched presentation")
        source = dict(block.get("source")) if isinstance(block.get("source"), Mapping) else {}
        source["ref"] = scope
        block["source"] = source
        block["generation"] = bound_generation
        block["ref"] = _bound_ref(scope, block_id, bound_generation, digest)
        result.append(block)
    return result


def provider_block_projection(text: str, blocks: Sequence[Mapping[str, object]], *, maximum_bytes: int = 8_192) -> str:
    """Return a deterministic bounded provider/compaction/RAG-safe projection."""

    lines = [text.strip()] if text.strip() else []
    seen: set[str] = set()
    for block in blocks:
        ref = _compact(block.get("ref"), 220)
        if not ref or ref in seen:
            continue
        seen.add(ref)
        line = (
            f"[内容块 ref={ref} type={_compact(block.get('type'), 80)}："
            f"{_compact(block.get('summary'), MAX_SUMMARY_CHARS)}]"
        )
        candidate = "\n".join([*lines, line])
        if len(candidate.encode("utf-8")) > maximum_bytes:
            break
        lines.append(line)
    return "\n".join(lines)


def validate_persisted_blocks(
    values: object,
    *,
    allowed_visibility: frozenset[str] = frozenset({"private_session", "room_post", "root_post"}),
) -> list[dict[str, object]]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_BLOCKS_PER_TURN:
        raise ValueError("Agent blocks must be a bounded array")
    result: list[dict[str, object]] = []
    total = 0
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping) or value.get("schemaVersion") != BLOCK_SCHEMA_VERSION:
            raise ValueError("Agent block schemaVersion is invalid")
        block = dict(value)
        block_ref = _compact(block.get("ref"), 240)
        digest = _compact(block.get("digest"), 64)
        visibility = _compact(block.get("visibility"), 40)
        if not block_ref or block_ref in seen or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Agent block ref/digest is invalid or duplicated")
        if visibility not in allowed_visibility:
            raise ValueError("Agent block visibility does not match this projection")
        if block.get("type") not in {*KNOWN_BLOCK_TYPES, "unknown"} or block.get("type") == "html_widget":
            raise ValueError("Agent block type is not allowed")
        if not _safe_data(block.get("data")):
            raise ValueError("Agent block data is unsafe")
        if digest != _content_digest(str(block.get("type") or ""), block.get("data")):
            raise ValueError("Agent block content digest does not match type/data")
        if block.get("summary") != _summary(str(block.get("type") or ""), dict(block.get("data") or {})):
            raise ValueError("Agent block summary does not match type/data")
        if block.get("presentationKind") != _presentation(str(block.get("type") or "")):
            raise ValueError("Agent block presentation does not match type")
        source = block.get("source") if isinstance(block.get("source"), Mapping) else {}
        expected_ref = _bound_ref(
            _compact(source.get("ref"), 240),
            _compact(block.get("id"), 160),
            max(0, int(block.get("generation") or 0)),
            digest,
        )
        if block_ref != expected_ref:
            raise ValueError("Agent block ref does not match its server-bound scope")
        encoded = _canonical_json(block).encode("utf-8")
        if len(encoded) > MAX_BLOCK_BYTES:
            raise ValueError("Agent block exceeds per-block byte budget")
        total += len(encoded)
        if total > MAX_TURN_BLOCK_BYTES:
            raise ValueError("Agent blocks exceed turn byte budget")
        seen.add(block_ref)
        result.append(block)
    return result


def _summary(block_type: str, data: Mapping[str, object]) -> str:
    title = _compact(data.get("title") or data.get("label") or data.get("name") or data.get("fileName") or data.get("path"), 120)
    if block_type == "card":
        return f"卡片：{title}" if title else "卡片"
    if block_type == "checklist":
        items = data.get("items") if isinstance(data.get("items"), list) else []
        done = sum(1 for item in items if isinstance(item, Mapping) and item.get("checked") is True)
        return f"清单{'：' + title if title else ''}，{done}/{len(items)} 完成"
    if block_type == "table":
        rows = len(data.get("rows")) if isinstance(data.get("rows"), list) else 0
        columns = len(data.get("columns")) if isinstance(data.get("columns"), list) else 0
        return f"表格{'：' + title if title else ''}，{rows} 行 {columns} 列"
    if block_type == "diff":
        return f"代码变更：{_compact(data.get('filePath') or data.get('path') or '未知文件', 140)}"
    if block_type in {"file", "artifact"}:
        return f"产物：{title or _compact(data.get('ref') or '已生成产物', 160)}"
    if block_type in {"citation", "reference"}:
        return f"引用：{title or _compact(data.get('ref') or data.get('source') or '已记录引用', 160)}"
    if block_type in {"status", "progress"}:
        return f"状态：{title or _compact(data.get('state') or data.get('summary') or '已更新', 160)}"
    if block_type == "unknown":
        return f"暂不支持的内容：{_compact(data.get('originalType') or 'unknown', 80)}"
    return _compact(data.get("summary") or data.get("text") or data.get("message") or title or f"已生成 {block_type} 内容", MAX_SUMMARY_CHARS)


def _presentation(block_type: str) -> str:
    if block_type == "text":
        return "markdown"  # Canonical text presentation from agent_protocol.
    if block_type in RICH_BLOCK_TYPES:
        return f"{block_type}.v1"
    return "unsupported" if block_type == "unknown" else block_type


def _safe_data(value: object, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return len(value.encode("utf-8")) <= MAX_BLOCK_BYTES
    if isinstance(value, list):
        return len(value) <= 256 and all(_safe_data(item, depth + 1) for item in value)
    if not isinstance(value, Mapping):
        return False
    for raw_key, item in value.items():
        key = str(raw_key).lower()
        if key in _FORBIDDEN_KEYS or key.startswith("on"):
            return False
        if (key == "url" or key.endswith("url")) and isinstance(item, str) and not _SAFE_URL.match(item):
            return False
        if not _safe_data(item, depth + 1):
            return False
    return True


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _content_digest(block_type: str, data: object) -> str:
    normalized_data = dict(data) if isinstance(data, Mapping) else {}
    content = {"type": str(block_type), "data": normalized_data}
    return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()


def _bound_ref(scope_ref: str, block_id: str, generation: int, digest: str) -> str:
    if not scope_ref or not block_id:
        return ""
    value = f"{scope_ref}\0{block_id}\0{max(0, int(generation))}\0{digest}"
    return f"block:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _compact(value: object, maximum: int) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:maximum]
