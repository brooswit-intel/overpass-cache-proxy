"""SQLite-backed cache and persisted hit/send counters.

A fresh ``Cache(path)`` against a path that doesn't exist yet starts with
zero cached entries and zero counters ("empty-start"). Everything here is
local disk I/O; nothing in this module makes a network call.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class CachedResponse:
    status: int
    content_type: str
    body: bytes


class Cache:
    """Thread-safe SQLite cache of complete, successful upstream responses.

    Counters are persisted in the same database so a before/after delta of
    :meth:`stats` is valid across a proxy restart, not just within one
    process's lifetime.
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS entries ("
            " key TEXT PRIMARY KEY,"
            " status INTEGER NOT NULL,"
            " content_type TEXT NOT NULL,"
            " body BLOB NOT NULL"
            ")"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS counters ("
            " name TEXT PRIMARY KEY,"
            " value INTEGER NOT NULL"
            ")"
        )
        for name in ("local_hits", "upstream_sends"):
            self._conn.execute(
                "INSERT OR IGNORE INTO counters (name, value) VALUES (?, 0)",
                (name,),
            )
        self._conn.commit()

    def get(self, key: str) -> CachedResponse | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, content_type, body FROM entries WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        status, content_type, body = row
        return CachedResponse(status=status, content_type=content_type, body=bytes(body))

    def put(self, key: str, response: CachedResponse) -> None:
        """Store a response. Callers must only call this for a complete,
        successful (HTTP 200) response — this module does not itself filter
        on status, so an incomplete or error response must never reach here.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO entries (key, status, content_type, body)"
                " VALUES (?, ?, ?, ?)",
                (key, response.status, response.content_type, response.body),
            )
            self._conn.commit()

    def record_local_hit(self) -> None:
        self._incr("local_hits")

    def record_upstream_send(self) -> None:
        self._incr("upstream_sends")

    def _incr(self, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE counters SET value = value + 1 WHERE name = ?", (name,)
            )
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT name, value FROM counters").fetchall()
        return {name: value for name, value in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
