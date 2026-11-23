"""SQLite connection, schema management and storage migration.

Schema version 2 no longer stores a full per-version snapshot of active
results.  The global registry (``results``) already records, for every
result, the version that added it (``added_version``) and the version that
withdrew it (``withdrawn_version``).  Because versions form a single linear
chain, the active set of any version ``v`` is exactly::

    added_version <= v AND (withdrawn_version IS NULL OR withdrawn_version > v)

so the snapshot table was pure duplication.  Publishing one announcement now
only inserts one registry row per supplement and updates one registry row per
withdrawal: storage growth follows the operation count, not the archive size.

``migrate_db`` upgrades a version-1 volume in a single transaction: it first
verifies that the registry reproduces every historical snapshot exactly, then
rebuilds ``version_progression`` without the foreign key to the snapshot
table, drops the snapshot table and stamps ``user_version = 2``.  Any failure
rolls the whole transaction back, so an interrupted or failed upgrade leaves
the original volume untouched: the previous service keeps working on it and
the next startup simply retries the migration.
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA_VERSION = 2

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

CREATE TABLE IF NOT EXISTS version_progression (
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('first', 'refresh', 'tie')),
    result_id TEXT NOT NULL,
    athlete TEXT NOT NULL,
    finish_time TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    PRIMARY KEY (version, seq),
    FOREIGN KEY (version) REFERENCES versions(version) ON DELETE CASCADE
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

-- Global registry of every result ever submitted.  ``added_version`` and
-- ``withdrawn_version`` delimit the versions in which the result is active,
-- which replaces the old per-version snapshot table: a withdrawal takes
-- effect from the version whose announcement carried it, and a withdrawn id
-- stays reserved forever.  Rows are never deleted.
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

-- Supports reconstructing the active set of any version from the registry.
CREATE INDEX IF NOT EXISTS ix_results_validity
    ON results(added_version, withdrawn_version);
"""


class MigrationError(RuntimeError):
    """The existing volume could not be upgraded; it was left untouched."""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection suitable for one short transaction."""

    connection = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def init_db(db_path: str) -> None:
    """Create the database directory (if needed) and all tables.

    A freshly created database is stamped with the current schema version.
    An existing database is left for :func:`migrate_db` to inspect, so its
    version marker still reflects the schema its data actually uses.
    """

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
        fresh = not _table_exists(connection, "versions")
        connection.executescript(SCHEMA)
        if fresh:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        connection.close()


def migrate_db(db_path: str) -> bool:
    """Upgrade an existing volume to the current schema version.

    Returns True if a migration ran.  The v1 -> v2 step merges the duplicated
    per-version result snapshots into the registry-derived representation and
    is fully transactional: on error nothing changes and the call can simply
    be retried on the next startup.  The state is inspected inside the write
    transaction, so a concurrent migrator cannot interleave.
    """

    connection = connect(db_path)
    try:
        if not _table_exists(connection, "versions"):
            # No data at all: just create the current schema.
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return True

        # Foreign-key enforcement is suspended for the table rebuild; the
        # pragma only takes effect outside a transaction.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current >= SCHEMA_VERSION:
                connection.execute("ROLLBACK")
                return False

            progression_fks = {
                row["table"]
                for row in connection.execute(
                    "PRAGMA foreign_key_list(version_progression)"
                )
            }
            needs_rebuild = "version_results" in progression_fks
            has_snapshots = _table_exists(connection, "version_results")
            if not needs_rebuild and not has_snapshots:
                # Already in the new shape (e.g. stamping was interrupted).
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.execute("COMMIT")
                return True

            if has_snapshots:
                _verify_snapshots_match_registry(connection)
            if needs_rebuild:
                _rebuild_progression_table(connection)
            if has_snapshots:
                connection.execute("DROP TABLE version_results")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("COMMIT")
            return True
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


def _verify_snapshots_match_registry(connection: sqlite3.Connection) -> None:
    """Fail the migration unless the registry reproduces every snapshot.

    The old per-version snapshots are the record being deduplicated; they are
    only dropped once the registry is proven to reconstruct them exactly,
    both in membership and in every stored field.
    """

    orphan = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM version_results vr
        LEFT JOIN results r ON r.result_id = vr.result_id
        WHERE r.result_id IS NULL
           OR r.added_version > vr.version
           OR (r.withdrawn_version IS NOT NULL
               AND r.withdrawn_version <= vr.version)
           OR r.athlete <> vr.athlete
           OR r.finish_time <> vr.finish_time
           OR r.duration_ms <> vr.duration_ms
        """
    ).fetchone()["n"]
    missing = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM versions v
        JOIN results r
          ON r.added_version <= v.version
         AND (r.withdrawn_version IS NULL OR r.withdrawn_version > v.version)
        LEFT JOIN version_results vr
          ON vr.version = v.version AND vr.result_id = r.result_id
        WHERE vr.result_id IS NULL
        """
    ).fetchone()["n"]
    inconsistent_status = connection.execute(
        """
        SELECT COUNT(*) AS n FROM results
        WHERE (status = 'withdrawn') <> (withdrawn_version IS NOT NULL)
        """
    ).fetchone()["n"]
    if orphan or missing or inconsistent_status:
        raise MigrationError(
            "登记簿与历史快照不一致，无法合并 "
            f"(快照多余/不一致 {orphan} 条，快照缺失 {missing} 条，"
            f"状态标记异常 {inconsistent_status} 条)；数据卷保持原样"
        )


def _rebuild_progression_table(connection: sqlite3.Connection) -> None:
    """Recreate ``version_progression`` without the snapshot-table FK."""

    connection.execute(
        """
        CREATE TABLE version_progression_v2 (
            version INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('first', 'refresh', 'tie')),
            result_id TEXT NOT NULL,
            athlete TEXT NOT NULL,
            finish_time TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            PRIMARY KEY (version, seq),
            FOREIGN KEY (version) REFERENCES versions(version)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        INSERT INTO version_progression_v2 (
            version, seq, kind, result_id, athlete, finish_time, duration_ms
        )
        SELECT version, seq, kind, result_id, athlete, finish_time, duration_ms
        FROM version_progression
        """
    )
    connection.execute("DROP TABLE version_progression")
    connection.execute(
        "ALTER TABLE version_progression_v2 RENAME TO version_progression"
    )
