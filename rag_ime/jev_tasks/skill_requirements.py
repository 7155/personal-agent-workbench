"""Parse explicit Jev task capabilities without treating Skill names as tools."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .types import GraphError


_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def split_required_capabilities(specification: Mapping[str, object]) -> tuple[list[str], list[str]]:
    required = specification.get("requiredCapabilities", [])
    if not isinstance(required, list) or len(required) > 24:
        raise GraphError("invalid requiredCapabilities")
    tools: list[str] = []
    skills: list[str] = []
    for value in required:
        if not isinstance(value, str) or not value:
            raise GraphError("invalid required capability")
        if value.startswith("skill:"):
            name = value.removeprefix("skill:")
            if _SKILL_NAME.fullmatch(name) is None:
                raise GraphError("invalid required Skill name")
            skills.append(name)
        else:
            tools.append(value)
    return tools, skills
