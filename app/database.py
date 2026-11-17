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

-- Station calibrations: zero-point counts and integer gains (microgals/count)
-- that take effect at a whole UTC second.  One row per (station, effective
-- second); revision semantics mirror the fragment table.
CREATE TABLE IF NOT EXISTS calibrations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    station      TEXT    NOT NULL,
    effective_at TEXT    NOT NULL,
    revision     INTEGER NOT NULL CHECK (revision >= 1),
    zero_count   INTEGER NOT NULL,
    gain         INTEGER NOT NULL CHECK (gain <> 0),
    content_hash TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (station, effective_at)
);

CREATE INDEX IF NOT EXISTS idx_calibrations_station
    ON calibrations (station, effective_at);

-- Conversion records: per-second microgal values derived from one pinned,
-- non-stale publication and the calibrations effective at each sample time.
CREATE TABLE IF NOT EXISTS conversions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    station              TEXT    NOT NULL,
    minute_utc           TEXT    NOT NULL,
    publication_id       INTEGER NOT NULL REFERENCES publications(id),
    publication_wave_hash TEXT   NOT NULL,
    calibration_set_hash TEXT    NOT NULL,
    values_json          TEXT    NOT NULL,
    stale                INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_conversions_minute
    ON conversions (station, minute_utc);

CREATE INDEX IF NOT EXISTS idx_conversions_publication
    ON conversions (publication_id);

-- Per-slot provenance of a conversion: which calibration revision converted
-- each sample, its coefficients and the resulting microgal value.
CREATE TABLE IF NOT EXISTS conversion_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    conversion_id  INTEGER NOT NULL REFERENCES conversions(id) ON DELETE CASCADE,
    sample         INTEGER NOT NULL CHECK (sample >= 0 AND sample < 60),
    effective_at   TEXT    NOT NULL,
    revision       INTEGER NOT NULL,
    zero_count     INTEGER NOT NULL,
    gain           INTEGER NOT NULL,
    value          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_items_conv
    ON conversion_items (conversion_id);
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
