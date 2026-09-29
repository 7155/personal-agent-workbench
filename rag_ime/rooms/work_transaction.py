"""Optional transaction hooks for one trusted task-kernel command.

The WorkItem owner still performs every state change and authorization check.
Hooks run *inside that owner's existing BEGIN IMMEDIATE*, not in a second
connection/transaction. With no scope set, legacy Room behavior is unchanged.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TransactionHooks:
    db_path: Path
    before: Callable[[sqlite3.Connection], None]
    after: Callable[[sqlite3.Connection], None]
    enlisted: bool = False
    finished: bool = False


_CURRENT: ContextVar[TransactionHooks | None] = ContextVar("paw_work_transaction", default=None)


@contextmanager
def guarded_work_transaction(hooks: TransactionHooks) -> Iterator[None]:
    if _CURRENT.get() is not None:
        raise RuntimeError("nested WorkItem command scope is not supported")
    token = _CURRENT.set(hooks)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def participate_in_work_transaction(conn: sqlite3.Connection, db_path: str | Path,
                                    *, immediate: bool) -> Iterator[None]:
    hooks = _CURRENT.get()
    if hooks is None or not immediate:
        yield
        return
    if Path(db_path).resolve() != hooks.db_path.resolve():
        raise RuntimeError("WorkItem command crossed its database boundary")
    if hooks.enlisted:
        raise RuntimeError("one kernel command cannot span multiple owner write transactions")
    if not conn.in_transaction:
        raise RuntimeError("guard requires the owner's active write transaction")
    hooks.enlisted = True
    hooks.before(conn)
    yield
    hooks.after(conn)
    hooks.finished = True
