"""SQLite schema and connection management."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS fragments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    station      TEXT    NOT NULL,
    minute_utc   TEXT    NOT NULL,
    fragment_no  INTEGER NOT NULL,
    revision     INTEGER NOT NULL CHECK (revision >= 1),
    start_sample INTEGER NOT NULL CHECK (start_sample >= 0 AND start_sample < 60),
    samples      TEXT    NOT NULL,
    content_hash TEXT    NOT NULL,
    UNIQUE (station, minute_utc, fragment_no)
);

CREATE INDEX IF NOT EXISTS idx_fragments_minute
    ON fragments (station, minute_utc);

CREATE TABLE IF NOT EXISTS publications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    station          TEXT    NOT NULL,
    minute_utc       TEXT    NOT NULL,
    wave             TEXT    NOT NULL,
    fragment_set_hash TEXT   NOT NULL,
    stale            INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_publications_minute
    ON publications (station, minute_utc);

CREATE TABLE IF NOT EXISTS publication_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    fragment_no    INTEGER NOT NULL,
    revision       INTEGER NOT NULL,
    start_sample   INTEGER NOT NULL,
    samples        TEXT    NOT NULL,
    content_hash   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pub_items_pub
    ON publication_items (publication_id);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # Writers take the write lock up front so concurrent uploads/publishes
    # serialize instead of failing with SQLITE_BUSY on commit.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: Path | None = None) -> None:
    """Create the schema if it does not exist yet (safe on every startup)."""
    path = db_path or DB_PATH
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a request-scoped connection; the service layer owns commits."""
    conn = _connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()
