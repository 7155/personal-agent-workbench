"""Bounded transcript file reads; no Runtime startup or Session mutation."""

from __future__ import annotations
import os
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from rag_ime.secure_files import regular_reader

__all__ = [
    "read_recent_transcript_tail",
    "transcript_boundary_sha256",
    "DURABLE_TRANSCRIPT_MAX_LINE_BYTES",
]
_RECENT_SESSION_TAIL_SCAN_BYTES = 2 * 1024 * 1024
DURABLE_TRANSCRIPT_MAX_LINE_BYTES = 8 * 1024 * 1024
_RECENT_TRANSCRIPT_BOUNDARY_BYTES = 64 * 1024


def _descriptor_path(path: Path) -> Path:
    """Normalize only macOS's root aliases before descriptor traversal.

    ``secure_files.parent_descriptor`` deliberately refuses symlink path
    components.  macOS presents ``/var``, ``/tmp`` and ``/etc`` as stable
    system aliases to ``/private/*`` though, so canonicalize those three
    prefixes without resolving any application-controlled descendant.  All
    descendant components still go through O_NOFOLLOW in ``regular_reader``.
    """

    absolute = path.expanduser().absolute()
    parts = absolute.parts
    if len(parts) > 1 and parts[1] in {"var", "tmp", "etc"}:
        name = parts[1]
        private_target = Path("/private") / name
        alias = Path("/") / name
        try:
            resolved_alias = alias.resolve(strict=False)
        except (OSError, RuntimeError):
            return absolute
        if resolved_alias == private_target:
            return private_target.joinpath(*parts[2:])
    return absolute


def read_recent_transcript_tail(
    transcript: Path,
    file_size: int,
) -> tuple[dict[str, object], list[dict[str, object]]] | None:
    """Read the Session header and one bounded suffix of complete JSONL rows."""

    # ``file_size`` is retained for the existing caller contract, but it is
    # only a hint.  The descriptor opened with O_NOFOLLOW is the authority for
    # the size and all subsequent seeks, so a concurrent replacement cannot
    # redirect the read to a symlink or another file.
    del file_size
    with regular_reader(_descriptor_path(transcript)) as source:
        actual_size = int(os.fstat(source.fileno()).st_size)
        header_line = source.readline(DURABLE_TRANSCRIPT_MAX_LINE_BYTES + 1)
        if not header_line or len(header_line) > DURABLE_TRANSCRIPT_MAX_LINE_BYTES:
            return None
        try:
            raw_header = json.loads(header_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(raw_header, Mapping):
            return None
        header_end = source.tell()
        tail_start = max(
            header_end,
            max(0, actual_size - _RECENT_SESSION_TAIL_SCAN_BYTES),
        )
        source.seek(tail_start)
        data = source.read(_RECENT_SESSION_TAIL_SCAN_BYTES)
    if tail_start > header_end:
        first_newline = data.find(b"\n")
        if first_newline < 0:
            return None
        data = data[first_newline + 1 :]
    entries: list[dict[str, object]] = []
    for line in data.splitlines():
        if len(line) > DURABLE_TRANSCRIPT_MAX_LINE_BYTES:
            return None
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        except UnicodeDecodeError:
            return None
        if isinstance(value, Mapping):
            entries.append(dict(value))
    return dict(raw_header), entries


def transcript_boundary_sha256(transcript: Path, end_offset: int) -> str:
    """Fingerprint a bounded prefix boundary to prove monotonic append."""

    bounded_end = max(0, int(end_offset))
    start = max(0, bounded_end - _RECENT_TRANSCRIPT_BOUNDARY_BYTES)
    with regular_reader(_descriptor_path(transcript)) as source:
        source.seek(start)
        payload = source.read(bounded_end - start)
    if len(payload) != bounded_end - start:
        raise OSError("transcript changed while reading append boundary")
    return hashlib.sha256(payload).hexdigest()
