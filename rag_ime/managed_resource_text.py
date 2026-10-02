"""Lossless, bounded line cursors for managed text resources."""
from __future__ import annotations


def readable_lines(content: str, max_bytes: int = 40 * 1024) -> tuple[list[str], bool]:
    """Keep ordinary lines; split oversized ones at UTF-8 character boundaries.

    A native read only has line cursors. Its returned nextLineOffset addresses
    these stable segments, so even a minified JSON receipt can be read fully.
    No newline or other byte is inserted into the original content.
    """
    lines = []
    segmented = False
    for line in content.splitlines(keepends=True):
        raw = line.encode('utf-8')
        if len(raw) <= max_bytes:
            lines.append(line)
            continue
        segmented = True
        start = 0
        while start < len(raw):
            end = min(len(raw), start + max_bytes)
            while end < len(raw) and raw[end] & 0xC0 == 0x80:
                end -= 1
            lines.append(raw[start:end].decode('utf-8'))
            start = end
    return lines, segmented
