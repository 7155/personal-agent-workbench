"""Independent current-schema databases for behavior tests.

Migration tests must continue constructing their own empty or legacy databases.
This fixture executes the real migration chain once and copies its pristine
result; product initializers and their normal SQLite durability remain active.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from rag_ime.db.migration_runner import Migration, apply_database_migrations, load_migrations


@lru_cache(maxsize=1)
def _pristine_database(migrations: tuple[Migration, ...]) -> bytes:
    with closing(sqlite3.connect(":memory:")) as source:
        apply_database_migrations(source, applied_at_ms=1)
        return source.serialize()


def copy_current_database(destination: Path) -> None:
    """Create a fresh independent database, refusing an existing target."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation makes a mistaken fixture path fail instead of
    # overwriting a populated database or a previous case's assertions.
    with destination.open("xb"):
        pass
    with closing(sqlite3.connect(":memory:")) as source:
        source.deserialize(_pristine_database(load_migrations()))
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
