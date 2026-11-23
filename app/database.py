"""SQLite connection and schema management."""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version INTEGER PRIMARY KEY,
    parent_version INTEGER,
    published_at TEXT NOT NULL,
    supplement_count INTEGER NOT NULL CHECK (supplement_count >= 0),
    withdrawal_count INTEGER NOT NULL CHECK (withdrawal_count >= 0),
    recompute_from_finish_time TEXT,
    reused_prefix_entries INTEGER NOT NULL CHECK (reused_prefix_entries >= 0),
    recomputed_active_results INTEGER NOT NULL CHECK (recomputed_active_results >= 0)
);

-- A version may have at most one child.  Together with the base-version check
-- in the publish transaction this enforces "two announcements based on the
-- same version: at most one may be published".
CREATE UNIQUE INDEX IF NOT EXISTS ux_versions_parent
    ON versions(parent_version);

CREATE TABLE IF NOT EXISTS version_results (
    version INTEGER NOT NULL,
    result_id TEXT NOT NULL,
    athlete TEXT NOT NULL,
    finish_time TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    PRIMARY KEY (version, result_id),
    FOREIGN KEY (version) REFERENCES versions(version) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_version_results_order
    ON version_results(version, finish_time, result_id);

CREATE TABLE IF NOT EXISTS version_progression (
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('first', 'refresh', 'tie')),
    result_id TEXT NOT NULL,
    athlete TEXT NOT NULL,
    finish_time TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    PRIMARY KEY (version, seq),
    FOREIGN KEY (version, result_id)
        REFERENCES version_results(version, result_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS version_operations (
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    op TEXT NOT NULL CHECK (op IN ('supplement', 'withdraw')),
    result_id TEXT NOT NULL,
    athlete TEXT,
    finish_time TEXT,
    duration_ms INTEGER,
    PRIMARY KEY (version, seq),
    FOREIGN KEY (version) REFERENCES versions(version) ON DELETE CASCADE
);

-- Current global registry.  It is used for uniqueness checks and for looking
-- up the details of a result named by a withdrawal.  Historical snapshots are
-- kept in version_results and never depend on this table.
CREATE TABLE IF NOT EXISTS results (
    result_id TEXT PRIMARY KEY,
    athlete TEXT NOT NULL,
    finish_time TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'withdrawn')),
    added_version INTEGER NOT NULL,
    withdrawn_version INTEGER,
    FOREIGN KEY (added_version) REFERENCES versions(version),
    FOREIGN KEY (withdrawn_version) REFERENCES versions(version)
);

CREATE INDEX IF NOT EXISTS ix_results_order
    ON results(status, finish_time, result_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection suitable for one short transaction."""

    connection = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def init_db(db_path: str) -> None:
    """Create the database directory (if needed) and all tables."""

    if db_path != ":memory:":
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    connection = connect(db_path)
    try:
        # WAL keeps old, committed snapshots readable while a new version is
        # being computed and written in a write transaction.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(SCHEMA)
    finally:
        connection.close()
