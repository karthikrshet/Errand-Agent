"""SQLite helpers shared by every store.

Connections run in autocommit mode (`isolation_level=None`) so that transactions are always
explicit. Writers use `BEGIN IMMEDIATE`, which takes the write lock up front: two concurrent
confirmations then serialise at the database instead of both reading "unconsumed" and racing.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class ConnectionPool:
    """One connection per thread for a file database.

    A single `sqlite3.Connection` shared across threads (FastAPI's threadpool plus the watcher)
    lets one thread's statements land inside another thread's `BEGIN IMMEDIATE`. Per-thread
    connections keep every transaction private; the database file is the shared state.
    An explicit connection or `:memory:` is used as-is, since a second in-memory connection
    would be a different database.
    """

    def __init__(self, db: Path | str | sqlite3.Connection) -> None:
        self._fixed = db if isinstance(db, sqlite3.Connection) else None
        self._path = "" if isinstance(db, sqlite3.Connection) else str(db)
        if self._path == ":memory:":
            self._fixed = connect(self._path)
        self._local = threading.local()

    def get(self) -> sqlite3.Connection:
        if self._fixed is not None:
            return self._fixed
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self._path)
            self._local.conn = conn
        return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
