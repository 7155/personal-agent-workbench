"""File previews rooted in one authorized space, without host-path access.

Walk through directory descriptors with O_NOFOLLOW. Checking Path.resolve and
opening the path later leaves a symlink-swap window while an Agent is writing.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import stat
from collections.abc import Iterator, Mapping


def _number(value: object, default: int, maximum: int, minimum: int = 0) -> int:
    try:
        result = default if value is None or value == '' else int(str(value))
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid file pagination') from exc
    if not minimum <= result <= maximum:
        raise ValueError('File pagination is out of bounds')
    return result


def _parts(value: object) -> tuple[str, ...]:
    raw = str(value or '/')
    if '\x00' in raw or '\\' in raw or len(raw) > 4096:
        raise ValueError('Invalid space path')
    parts = tuple(part for part in raw.split('/') if part not in {'', '.'})
    if '..' in parts:
        raise PermissionError('Path escapes the selected space')
    return parts


def _virtual(parts: tuple[str, ...]) -> str:
    return '/' + '/'.join(parts)


class TeamFiles:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('Space file root must be a directory')

    @contextmanager
    def _open(self, parts: tuple[str, ...], *, directory: bool = False) -> Iterator[int]:
        descriptors: list[int] = []
        try:
            current = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(current)
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                current = os.open(part, flags, dir_fd=current)
                descriptors.append(current)
            yield current
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise PermissionError('Links cannot leave or redirect a space path') from exc
            raise
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def list(self, args: Mapping[str, object]) -> dict[str, object]:
        parts = _parts(args.get('path'))
        offset = _number(args.get('offset'), 0, 10_000_000)
        limit = _number(args.get('limit'), 240, 300, 1)
        entries: list[dict[str, object]] = []
        with self._open(parts, directory=True) as descriptor, os.scandir(descriptor) as iterator:
            for entry in iterator:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'symlink'
                entries.append({
                    'path': _virtual((*parts, entry.name)), 'name': entry.name, 'kind': kind,
                    **({'byteSize': info.st_size} if kind == 'file' else {}),
                })
        entries.sort(key=lambda item: (item['kind'] != 'directory', str(item['name']).casefold()))
        end = min(offset + limit, len(entries))
        return {
            'ok': True, 'scope': 'team', 'path': _virtual(parts),
            'parentPath': _virtual(parts[:-1]), 'homePath': '/', 'selectedPath': '',
            'items': entries[offset:end], 'truncated': end < len(entries),
            'nextOffset': end if end < len(entries) else None,
        }

    def read(self, args: Mapping[str, object]) -> dict[str, object]:
        parts = _parts(args.get('path'))
        offset = _number(args.get('offset'), 0, 2**53 - 1)
        limit = _number(args.get('limit'), 65_536, 65_536, 1)
        with self._open(parts) as descriptor:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Only ordinary files can be previewed')
            if offset > before.st_size:
                raise ValueError('File changed; refresh the preview')
            raw = os.pread(descriptor, limit, offset)
            if b'\x00' in raw:
                raise ValueError('Binary file cannot be previewed as text')
            try:
                content = raw.decode('utf-8')
            except UnicodeDecodeError as exc:
                if exc.reason != 'unexpected end of data' or offset + len(raw) >= before.st_size:
                    raise ValueError('File is not UTF-8 text') from exc
                content = raw[:exc.start].decode('utf-8')
            loaded = len(content.encode('utf-8'))
            if not loaded and offset < before.st_size:
                raise ValueError('Read limit is too short for one character')
            after = os.fstat(descriptor)
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if identity(before) != identity(after):
                raise ValueError('File changed; refresh the preview')
        revision = hashlib.sha256(repr(identity(before)).encode()).hexdigest()
        return {
            'ok': True, 'scope': 'team', 'path': _virtual(parts), 'requestedPath': _virtual(parts),
            'content': content, 'byteSize': before.st_size, 'offset': offset,
            'nextOffset': offset + loaded, 'truncated': offset + loaded < before.st_size,
            'resourceRevision': 'stat:' + revision,
            'editability': {'editable': False, 'reason': '在项目任务中修改文件，再分享新版本。'},
        }
