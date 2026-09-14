"""Descriptor-relative regular-file operations for writable workspace trees."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import uuid


@contextmanager
def parent_descriptor(path: Path, *, create: bool = False):
    absolute = path.absolute()
    if '..' in absolute.parts or absolute == Path('/'):
        raise ValueError('A normalized file path is required')
    current = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        yield current, absolute.name
    finally:
        os.close(current)


@contextmanager
def regular_reader(path: Path):
    with parent_descriptor(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Workspace content must be a regular file')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            yield source
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes) -> None:
    with parent_descriptor(path, create=True) as (parent, name):
        temporary = '.paw-' + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def unlink_file(path: Path) -> None:
    with parent_descriptor(path) as (parent, name):
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)


def replace_file(source: Path, target: Path) -> None:
    with parent_descriptor(source) as (source_parent, source_name), parent_descriptor(target, create=True) as (target_parent, target_name):
        os.replace(source_name, target_name, src_dir_fd=source_parent, dst_dir_fd=target_parent)
        os.fsync(source_parent)
        os.fsync(target_parent)


__all__ = ['parent_descriptor', 'regular_reader', 'atomic_write', 'unlink_file', 'replace_file']
