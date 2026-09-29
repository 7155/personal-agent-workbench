"""Validation shared by the existing four organization endpoints.

Nothing in this module grants access, classifies running state, or executes an Agent.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

CATEGORIES = {
    "active": "正在推进的工作", "waiting": "明确等待条件的工作",
    "incubating": "构思或尚待展开的想法", "reference": "可供查阅的资料讨论",
    "unknown": "仅从标题无法判断用途",
}
DEFAULT = {"category": "unknown", "placement": "desk", "group": "", "pinned": False}
MAX_REVISION = 9_007_199_254_740_991


class OrganizationConflict(ValueError):
    pass


class OrganizationDataError(Exception):
    """Stored metadata is invalid. Never silently turn it into an empty desktop."""


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fields(payload: object, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("整理请求字段无效，请刷新应用后重试。")
    return payload


def text(value: object, label: str, maximum: int, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum
            or (not empty and (not value or not value.strip()))
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError(f"{label}无效。")
    return value


def space_key(value: object) -> str:
    key = text(value, "空间标识", 300)
    kind, sep, identity = key.partition(":")
    if not sep or kind not in {"session", "room"} or not identity.strip() or identity != identity.strip():
        raise ValueError("空间标识无效。")
    return key


def revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_REVISION:
        raise ValueError("整理版本无效。")
    return value


def next_revision(value: int) -> int:
    if value >= MAX_REVISION:
        raise OrganizationConflict("整理版本已达到上限，请检查数据。")
    return value + 1


def validate_metadata(value: object, stored_revision: object) -> dict[str, Any]:
    try:
        if not isinstance(value, Mapping) or set(value) != set(DEFAULT):
            raise ValueError("shape")
        category, placement = value["category"], value["placement"]
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ValueError("category")
        if not isinstance(placement, str) or placement not in {"desk", "shelf"}:
            raise ValueError("placement")
        if not isinstance(value["pinned"], bool) or (value["pinned"] and placement == "shelf"):
            raise ValueError("pinned")
        text(value["group"], "分组", 80, empty=True)
        return {**value, "revision": revision(stored_revision)}
    except (TypeError, ValueError) as exc:
        raise OrganizationDataError("整理数据格式异常；原始对话未修改，请检查数据库。") from exc


def stored_metadata(raw: str, stored_revision: object) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise OrganizationDataError("整理数据无法读取；原始对话未修改。") from exc
    return validate_metadata(parsed, stored_revision)


def command_fields(payload: object) -> Mapping[str, Any]:
    p = fields(payload, {"commandId", "spaceKey", "expectedRevision", "operation", "value"})
    text(p["commandId"], "操作标识", 100)
    space_key(p["spaceKey"])
    revision(p["expectedRevision"])
    op, value = p["operation"], p["value"]
    if not isinstance(op, str):
        raise ValueError("整理操作无效。")
    if op == "category":
        if not isinstance(value, str) or value not in CATEGORIES:
            raise ValueError("用途无效。")
    elif op == "placement":
        if not isinstance(value, str) or value not in {"desk", "shelf"}:
            raise ValueError("空间位置无效。")
    elif op == "pinned":
        if not isinstance(value, bool):
            raise ValueError("固定状态必须为布尔值。")
    elif op == "group":
        text(value, "分组", 80, empty=True)
    elif op == "proposal":
        text(value, "建议标识", 100)
    else:
        raise ValueError("不支持此整理操作。")
    return p


def parse_choice(response: object) -> tuple[str, dict[str, Any]]:
    """Check transport shape, not semantic truth. Threshold remains policy."""
    if (not isinstance(response, Mapping)
            or not isinstance(response.get("model"), str) or not response["model"]):
        raise RuntimeError("Invalid Jev response")
    answers = response.get("answers")
    answer = answers.get("category") if isinstance(answers, Mapping) else None
    if not isinstance(answer, Mapping) or answer.get("type") != "choice":
        raise RuntimeError("Invalid Jev answer")
    choice, confidence, probs = answer.get("choice"), answer.get("confidence"), answer.get("probabilities")
    def probability(value: object) -> bool:
        return (isinstance(value, (float, int)) and not isinstance(value, bool)
                and 0 <= value <= 1 and math.isfinite(value))
    if (not isinstance(choice, str) or choice not in CATEGORIES or not probability(confidence)
            or not isinstance(probs, Mapping) or set(probs) != set(CATEGORIES)
            or not all(probability(p) for p in probs.values())
            or not math.isclose(sum(probs.values()), 1, rel_tol=0, abs_tol=0.0001)
            or probs[choice] + 0.000001 < max(probs.values())):
        raise RuntimeError("Invalid Jev choice")
    # Persist only the documented fields; do not retain arbitrary provider payloads.
    return choice, {"type": "choice", "choice": choice, "confidence": confidence,
                    "probabilities": dict(probs)}
