"""Tests for the v1 -> v2 storage migration and deduplicated storage growth.

The v1 schema stored a full snapshot of every version's active results in
``version_results``.  Version 2 derives any version's active set from the
registry's ``added_version``/``withdrawn_version`` interval instead.  These
tests build realistic v1 volumes by replaying the old publish writes, then
check that:

* migration merges the duplicated snapshots without changing any answer;
* a failed or interrupted migration leaves the volume byte-for-byte usable
  by the old service and can simply be retried;
* new publishes only add one registry row per supplement (and none for a
  withdrawal), so storage grows with the operation count.
"""

from __future__ import annotations

import sqlite3

import pytest

import app.database as database
import app.service as service
from app.errors import AppError
from app.progression import Result, compute_progression
from app.schemas import AnnouncementIn

# The schema the previously deployed service created, kept verbatim so the
# tests build genuine old-format volumes.
V1_SCHEMA = """
CREATE TABLE versions (
    version INTEGER PRIMARY KEY,
    parent_version INTEGER,
    published_at TEXT NOT NULL,
    supplement_count INTEGER NOT NULL CHECK (supplement_count >= 0),
    withdrawal_count INTEGER NOT NULL CHECK (withdrawal_count >= 0),
    recompute_from_finish_time TEXT,
    reused_prefix_entries INTEGER NOT NULL CHECK (reused_prefix_entries >= 0),
    recomputed_active_results INTEGER NOT NULL CHECK (recomputed_active_results >= 0)
);
CREATE UNIQUE INDEX ux_versions_parent ON versions(parent_version);
CREATE TABLE version_results (
    version INTEGER NOT NULL,
    result_id TEXT NOT NULL,
    athlete TEXT NOT NULL,
    finish_time TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    PRIMARY KEY (version, result_id),
    FOREIGN KEY (version) REFERENCES versions(version) ON DELETE CASCADE
);
CREATE INDEX ix_version_results_order
    ON version_results(version, finish_time, result_id);
CREATE TABLE version_progression (
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
CREATE TABLE version_operations (
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
CREATE TABLE results (
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
CREATE INDEX ix_results_order ON results(status, finish_time, result_id);
"""

PUBLISHED_AT = "2026-05-01T00:00:00.000000Z"


def _v1_publish(
    connection: sqlite3.Connection,
    supplements: list[tuple[str, str, str, int]],
    withdrawals: list[str],
) -> int:
    """Replay one old-style publish: full snapshot copy per version."""

    row = connection.execute("SELECT MAX(version) AS v FROM versions").fetchone()
    current = int(row[0]) if row[0] is not None else 0
    new_version = current + 1

    active = {
        r[0]: r
        for r in connection.execute(
            "SELECT result_id, athlete, finish_time, duration_ms "
            "FROM version_results WHERE version = ?",
            (current,),
        )
    }
    for result_id, athlete, finish_time, duration_ms in supplements:
        active[result_id] = (result_id, athlete, finish_time, duration_ms)
    withdrawn_rows = [active.pop(result_id) for result_id in withdrawals]

    connection.execute(
        """
        INSERT INTO versions (
            version, parent_version, published_at,
            supplement_count, withdrawal_count,
            recompute_from_finish_time,
            reused_prefix_entries, recomputed_active_results
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_version,
            current if current else None,
            PUBLISHED_AT,
            len(supplements),
            len(withdrawals),
            None,
            0,
            len(active),
        ),
    )
    connection.executemany(
        "INSERT INTO version_results VALUES (?, ?, ?, ?, ?)",
        [(new_version, *row) for row in active.values()],
    )
    progression = compute_progression(
        [
            Result(
                result_id=row[0], athlete=row[1], finish_time=row[2],
                duration_ms=row[3],
            )
            for row in active.values()
        ]
    )
    connection.executemany(
        "INSERT INTO version_progression VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (new_version, e.seq, e.kind, e.result_id, e.athlete,
             e.finish_time, e.duration_ms)
            for e in progression
        ],
    )
    operations = [
        (new_version, seq, "supplement", *row)
        for seq, row in enumerate(supplements, start=1)
    ]
    operations += [
        (new_version, seq, "withdraw", *row)
        for seq, row in enumerate(withdrawn_rows, start=len(supplements) + 1)
    ]
    connection.executemany(
        "INSERT INTO version_operations VALUES (?, ?, ?, ?, ?, ?, ?)",
        operations,
    )
    connection.executemany(
        "INSERT INTO results VALUES (?, ?, ?, ?, 'active', ?, NULL)",
        [(row[0], row[1], row[2], row[3], new_version) for row in supplements],
    )
    for result_id in withdrawals:
        connection.execute(
            "UPDATE results SET status = 'withdrawn', withdrawn_version = ? "
            "WHERE result_id = ?",
            (new_version, result_id),
        )
    return new_version


def _build_v1_volume(db_path: str) -> None:
    """A four-version old-format archive, including one withdrawal."""

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(V1_SCHEMA)
    connection.execute(
        """
        INSERT INTO versions (
            version, parent_version, published_at,
            supplement_count, withdrawal_count,
            recompute_from_finish_time,
            reused_prefix_entries, recomputed_active_results
        ) VALUES (1, NULL, ?, 0, 0, NULL, 0, 0)
        """,
        (PUBLISHED_AT,),
    )
    _v1_publish(
        connection,
        supplements=[
            ("R-001", "甲", "2026-05-01T10:00:00.000000Z", 250_000),
            ("R-002", "乙", "2026-05-02T10:00:00.000000Z", 240_000),
            ("R-003", "丙", "2026-05-03T10:00:00.000000Z", 240_000),
        ],
        withdrawals=[],
    )
    _v1_publish(
        connection,
        supplements=[("R-004", "丁", "2026-05-04T10:00:00.000000Z", 245_000)],
        withdrawals=[],
    )
    _v1_publish(connection, supplements=[], withdrawals=["R-002"])
    connection.commit()
    connection.close()


def _dump(db_path: str) -> dict:
    """Full logical content of the database, for equality comparisons."""

    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        content = {
            name: sorted(
                connection.execute(f"SELECT * FROM {name}").fetchall()
            )
            for name in tables
        }
        content["__user_version__"] = connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0]
        return content
    finally:
        connection.close()


def _old_view(db_path: str, version: int) -> list[tuple]:
    """What the old service returned as a version's active results."""

    connection = sqlite3.connect(db_path)
    try:
        return sorted(
            connection.execute(
                "SELECT result_id, athlete, finish_time, duration_ms "
                "FROM version_results WHERE version = ? "
                "ORDER BY finish_time, result_id",
                (version,),
            ).fetchall()
        )
    finally:
        connection.close()


def test_migration_merges_duplicate_snapshots_and_preserves_answers(tmp_path):
    db_path = str(tmp_path / "archive.db")
    _build_v1_volume(db_path)

    # The old service's answers, captured before the upgrade.
    old_active = {v: _old_view(db_path, v) for v in (1, 2, 3, 4)}
    old_dump = _dump(db_path)
    # The duplicated per-version copies the migration gets rid of.
    assert len(old_dump["version_results"]) == 3 + 4 + 3

    assert database.migrate_db(db_path) is True

    after = _dump(db_path)
    assert after["__user_version__"] == database.SCHEMA_VERSION
    assert "version_results" not in after
    # Every remaining table is untouched by the merge.
    for name, rows in after.items():
        if name in ("version_results", "__user_version__"):
            continue
        assert rows == old_dump[name], name

    # Published versions answer exactly as before: announcements, active
    # results and their counts, and the progression.
    for version, expected in old_active.items():
        detail = service.get_version(db_path, version)
        got = [
            (r["result_id"], r["athlete"], r["finish_time"], r["duration_ms"])
            for r in detail["active_results"]
        ]
        assert got == expected
        assert detail["active_result_count"] == len(expected)
    summaries = {row["version"]: row for row in service.list_versions(db_path)}
    assert [summaries[v]["active_result_count"] for v in (1, 2, 3, 4)] == [0, 3, 4, 3]
    assert [summaries[v]["progression_entry_count"] for v in (1, 2, 3, 4)] == [0, 3, 3, 2]

    # The registry still holds exactly one row per result ever submitted.
    assert len(after["results"]) == 4

    # Migration is idempotent.
    assert database.migrate_db(db_path) is False


def test_failed_migration_leaves_old_volume_usable_and_retryable(tmp_path):
    db_path = str(tmp_path / "archive.db")
    _build_v1_volume(db_path)
    before = _dump(db_path)

    # Force the migration to fail halfway through: a stray object occupies
    # the name of the rebuild table.
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE version_progression_v2 (x INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(sqlite3.OperationalError):
        database.migrate_db(db_path)

    # The volume is exactly as the old service left it: same tables, same
    # rows, same (unset) schema version — the old service keeps working.
    blocked = _dump(db_path)
    blocked.pop("version_progression_v2")  # the injected blocker itself
    assert blocked == before
    assert blocked["__user_version__"] == 0
    assert _old_view(db_path, 2) == [
        ("R-001", "甲", "2026-05-01T10:00:00.000000Z", 250_000),
        ("R-002", "乙", "2026-05-02T10:00:00.000000Z", 240_000),
        ("R-003", "丙", "2026-05-03T10:00:00.000000Z", 240_000),
    ]

    # Next startup retries the upgrade and succeeds.
    connection = sqlite3.connect(db_path)
    connection.execute("DROP TABLE version_progression_v2")
    connection.commit()
    connection.close()
    assert database.migrate_db(db_path) is True
    migrated = _dump(db_path)
    assert migrated["__user_version__"] == database.SCHEMA_VERSION
    assert "version_results" not in migrated
    for name in ("versions", "version_progression", "version_operations", "results"):
        assert migrated[name] == before[name]


def test_migration_aborts_when_registry_disagrees_with_snapshots(tmp_path):
    db_path = str(tmp_path / "archive.db")
    _build_v1_volume(db_path)

    # Corrupt the registry so it no longer reproduces the snapshots.
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE results SET finish_time = '1999-01-01T00:00:00.000000Z' "
        "WHERE result_id = 'R-003'"
    )
    connection.commit()
    connection.close()
    corrupted = _dump(db_path)

    with pytest.raises(database.MigrationError):
        database.migrate_db(db_path)

    # Nothing was merged or dropped; the upgrade can be fixed and retried.
    assert _dump(db_path) == corrupted

    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE results SET finish_time = '2026-05-03T10:00:00.000000Z' "
        "WHERE result_id = 'R-003'"
    )
    connection.commit()
    connection.close()
    assert database.migrate_db(db_path) is True
    assert _dump(db_path)["__user_version__"] == database.SCHEMA_VERSION


def test_withdrawal_semantics_and_id_reuse_on_migrated_volume(tmp_path):
    db_path = str(tmp_path / "archive.db")
    _build_v1_volume(db_path)  # R-002 withdrawn by version 4
    database.migrate_db(db_path)

    # The withdrawal is effective from the version that published it:
    # R-002 is still active in version 3 and gone from version 4.
    v3_ids = {r["result_id"] for r in service.get_version(db_path, 3)["active_results"]}
    v4_ids = {r["result_id"] for r in service.get_version(db_path, 4)["active_results"]}
    assert "R-002" in v3_ids
    assert "R-002" not in v4_ids

    # The registry still refuses to reuse the withdrawn id.
    with pytest.raises(AppError) as excinfo:
        service.publish(
            db_path,
            AnnouncementIn(
                base_version=4,
                supplements=[
                    {
                        "result_id": "R-002",
                        "athlete": "新人",
                        "finish_time": "2026-05-05T10:00:00Z",
                        "duration_ms": 230_000,
                    }
                ],
            ),
        )
    assert excinfo.value.code == "retired_result_id"

    # A new publish on the migrated volume works and stores no snapshot copy.
    new_version = service.publish(
        db_path,
        AnnouncementIn(
            base_version=4,
            supplements=[
                {
                    "result_id": "R-005",
                    "athlete": "戊",
                    "finish_time": "2026-05-05T10:00:00Z",
                    "duration_ms": 235_000,
                }
            ],
        ),
    )
    assert new_version == 5
    detail = service.get_version(db_path, new_version)
    assert detail["active_result_count"] == 4
    connection = database.connect(db_path)
    try:
        assert not database._table_exists(connection, "version_results")
    finally:
        connection.close()


def test_storage_grows_only_with_operation_count(tmp_path):
    """Consecutive small announcements: growth follows ops, not archive size."""

    db_path = str(tmp_path / "growth.db")
    database.init_db(db_path)
    service.ensure_baseline(db_path)

    def row_counts() -> dict[str, int]:
        connection = database.connect(db_path)
        try:
            return {
                name: connection.execute(
                    f"SELECT COUNT(*) AS n FROM {name}"
                ).fetchone()["n"]
                for name in (
                    "versions",
                    "version_progression",
                    "version_operations",
                    "results",
                )
            }
        finally:
            connection.close()

    connection = database.connect(db_path)
    try:
        assert not database._table_exists(connection, "version_results")
    finally:
        connection.close()

    before = row_counts()
    # Five announcements of one supplement each: the old schema would have
    # stored 1+2+3+4+5 = 15 snapshot rows; now only the registry grows.
    for index in range(5):
        service.publish(
            db_path,
            AnnouncementIn(
                base_version=index + 1,
                supplements=[
                    {
                        "result_id": f"G-{index:03d}",
                        "athlete": "选手",
                        "finish_time": f"2026-05-0{index + 1}T10:00:00Z",
                        "duration_ms": 250_000 - index * 1_000,
                    }
                ],
            ),
        )
    after_supplements = row_counts()
    assert after_supplements["results"] - before["results"] == 5
    assert after_supplements["version_operations"] - before["version_operations"] == 5

    # A withdrawal-only announcement adds no registry row at all: it updates
    # the withdrawn_version of the existing row in place.
    service.publish(
        db_path,
        AnnouncementIn(base_version=6, withdrawals=[{"result_id": "G-000"}]),
    )
    after_withdrawal = row_counts()
    assert after_withdrawal["results"] == after_supplements["results"]
    assert after_withdrawal["version_operations"] == after_supplements["version_operations"] + 1

    # Historical counts still reflect each version's active set.
    counts = [
        row["active_result_count"] for row in service.list_versions(db_path)
    ]
    assert counts == [0, 1, 2, 3, 4, 5, 4]


def test_fresh_database_is_stamped_and_needs_no_migration(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    database.init_db(db_path)
    assert _dump(db_path)["__user_version__"] == database.SCHEMA_VERSION
    assert database.migrate_db(db_path) is False
