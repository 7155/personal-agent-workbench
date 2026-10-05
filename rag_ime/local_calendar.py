"""The configured, replayable calendar shared by daily memory and timelines."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _zone_name(value: str) -> str:
    name = value.strip().removeprefix(":")
    if name.startswith("/"):
        _, separator, name = str(Path(name).resolve()).partition("/zoneinfo/")
        if not separator:
            raise ValueError("timezone file must identify an IANA zoneinfo name")
    # POSIX commonly spells the unchanging UTC calendar this way.
    return "UTC" if name == "UTC0" else name


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(_zone_name(value))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"unknown IANA timezone: {value}") from exc


def _system_timezone_names() -> list[str]:
    """Read OS timezone identity, never infer a region from an abbreviation."""
    names: list[str] = []
    try:
        localtime = Path("/etc/localtime").resolve(strict=True)
        _, separator, name = str(localtime).partition("/zoneinfo/")
        if separator:
            names.append(name)
    except OSError:
        pass
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if name:
            names.append(name)
    except OSError:
        pass
    return names


def resolve_calendar_timezone(value: str = "") -> ZoneInfo:
    """Prefer an explicit zone, then user configuration, then the OS calendar.

    ``astimezone().tzinfo`` is commonly a fixed-offset object with no ``key``.
    The OS's IANA identity preserves historical and future DST transitions;
    substituting its current offset or assuming Shanghai would change dates.
    """
    for configured in (value, os.environ.get("RAG_IME_TIMEZONE", ""), os.environ.get("TZ", "")):
        if configured.strip():
            return _zone(configured)
    local = datetime.now().astimezone().tzinfo
    names = [str(getattr(local, "key", "") or ""), *_system_timezone_names()]
    for name in filter(None, names):
        try:
            return _zone(name)
        except ValueError:
            continue
    # Do not map GMT to UTC: London can currently report GMT while still
    # needing summer-time transitions. An actual UTC identity is unambiguous.
    if local is not None and local.tzname(None) == "UTC":
        return ZoneInfo("UTC")
    raise ValueError("Cannot determine the local IANA timezone; set RAG_IME_TIMEZONE")
