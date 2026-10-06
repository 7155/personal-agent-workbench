#!/usr/bin/env python3
"""Archive inactive PAW install rollback bundles to a configured external disk.

The archive location is read from RAG_IME_APP_BACKUP_ARCHIVE_ROOT or from
~/Library/Application Support/RagIme/app-backup-archive-root.txt. No location
means no archival. The installer always keeps its two newest local backups.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def configured_root(home: Path) -> Path | None:
    configured = os.environ.get("RAG_IME_APP_BACKUP_ARCHIVE_ROOT")
    if configured is None:
        config = home / "Library/Application Support/RagIme/app-backup-archive-root.txt"
        if not config.is_file():
            return None
        configured = config.read_text(encoding="utf-8").strip()
    return Path(configured).expanduser() if configured else None


def has_open_files(path: Path) -> bool:
    result = subprocess.run(
        ["lsof", "-nP", "+D", str(path)], capture_output=True, text=True, timeout=60
    )
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError(f"cannot check open files in {path}: {result.stderr.strip()}")
    return bool(result.stdout.strip())


def same_content(source: Path, archive: Path) -> bool:
    result = subprocess.run(
        ["/usr/bin/rsync", "-anrc", "--delete", "--exclude=._*",
         str(source) + "/", str(archive) + "/"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and not result.stdout.strip()


def record(receipt: Path, **fields: object) -> None:
    with receipt.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": time.time(), **fields}, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def archive_backups(apps_dir: Path, archive_root: Path, *, require_external: bool = True) -> int:
    if not archive_root.is_dir() or archive_root.is_symlink():
        raise RuntimeError(f"archive location is unavailable: {archive_root}")
    if require_external and archive_root.stat().st_dev == apps_dir.stat().st_dev:
        raise RuntimeError(f"archive location is on the system disk: {archive_root}")
    backups = sorted(
        (p for p in apps_dir.glob(".paw-update.*") if p.is_dir() and not p.is_symlink()
         and not p.name.endswith(".archiving")),
        key=lambda p: p.stat().st_mtime,
    )
    receipt = archive_root / "migration.jsonl"
    archived = 0
    for source in backups[:-2]:
        if time.time() - source.stat().st_mtime < 3600 or has_open_files(source):
            continue
        destination = archive_root / source.name
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise RuntimeError(f"invalid archive destination: {destination}")
        if not destination.exists():
            if shutil.disk_usage(archive_root).free < 5 * 1024**3:
                raise RuntimeError("external archive has less than 5 GiB free")
            subprocess.run(
                ["/usr/bin/ditto", "--rsrc", "--extattr", str(source), str(destination)],
                check=True,
            )
        before = source.stat()
        if not same_content(source, destination):
            raise RuntimeError(f"archive content mismatch: {source}")
        if (before.st_mtime_ns, before.st_mode) != (
            destination.stat().st_mtime_ns, destination.stat().st_mode
        ):
            raise RuntimeError(f"archive metadata mismatch: {source}")
        if source.stat().st_mtime_ns != before.st_mtime_ns or has_open_files(source):
            continue
        if not same_content(source, destination):
            continue
        record(receipt, source=str(source), archive=str(destination), verified=True, removed=False)
        staged = source.with_name(source.name + ".archiving")
        if staged.exists():
            raise RuntimeError(f"incomplete previous archival needs review: {staged}")
        source.rename(staged)
        try:
            source.symlink_to(destination, target_is_directory=True)
        except Exception:
            staged.rename(source)
            raise
        shutil.rmtree(staged)
        record(receipt, source=str(source), archive=str(destination), verified=True, removed=True)
        archived += 1
    return archived


def main() -> int:
    home = Path.home()
    archive_root = configured_root(home)
    if archive_root is None:
        return 0
    try:
        count = archive_backups(home / "Applications", archive_root)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"PAW backup archival skipped: {error}", file=sys.stderr)
        return 1
    print(f"PAW archived {count} inactive install backup(s) to {archive_root}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
