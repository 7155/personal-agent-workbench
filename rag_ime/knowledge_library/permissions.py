from __future__ import annotations

import os
import stat
from pathlib import Path


KNOWLEDGE_DIRECTORY_MODE = 0o700
KNOWLEDGE_FILE_MODE = 0o600


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=KNOWLEDGE_DIRECTORY_MODE)
    if path.is_symlink() or not path.is_dir():
        raise OSError(f"knowledge storage directory is not a regular directory: {path}")
    os.chmod(path, KNOWLEDGE_DIRECTORY_MODE)


def secure_file(path: Path) -> None:
    if path.is_symlink():
        raise OSError(f"knowledge storage file must not be a symlink: {path}")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise OSError(f"knowledge storage path is not a regular file: {path}")
    os.chmod(path, KNOWLEDGE_FILE_MODE)


def harden_knowledge_tree(root: Path) -> None:
    """Create owner-only storage and migrate existing knowledge files in place."""

    secure_directory(root)
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        secure_directory(current_path)
        safe_directories: list[str] = []
        for name in directories:
            child = current_path / name
            if child.is_symlink():
                continue
            secure_directory(child)
            safe_directories.append(name)
        directories[:] = safe_directories
        for name in files:
            child = current_path / name
            if child.is_symlink():
                continue
            try:
                secure_file(child)
            except FileNotFoundError:
                # SQLite removes these transient files when its last handle
                # closes. A concurrent reader may have listed them just before
                # that close; retain failures for persistent source files.
                if not name.endswith(("-wal", "-shm")):
                    raise
