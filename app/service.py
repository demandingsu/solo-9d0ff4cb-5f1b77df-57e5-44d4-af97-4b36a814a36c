"""Transactional service layer.

Every publish is a single SQLite ``BEGIN IMMEDIATE`` transaction that:

1. verifies the announcement's base version is still the latest version;
2. validates all operations against the base snapshot and the global registry;
3. computes the new progression, reusing the unaffected prefix;
4. writes the new version, its full snapshot, its progression and its
   operations, and updates the global registry;
5. commits atomically.

Any validation error, stale base version or write failure rolls the whole
transaction back, so previously published versions remain exactly as they
were.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from app.announcement import OperationSeed, apply_announcement
from app.database import connect
from app.errors import AppError
from app.progression import (
    ProgressionEntry,
    Result,
    compute_from,
    standing_from_prefix,
)
from app.schemas import AnnouncementIn
from app.timeutil import utc_now

MAX_PAGE_SIZE = 200


def _baseline_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT 1 FROM versions LIMIT 1").fetchone()
    return row is not None


def ensure_baseline(db_path: str) -> None:
    """Create the empty version 1 baseline if the archive has no versions."""

    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if not _baseline_exists(connection):
            connection.execute(
                """
                INSERT INTO versions (
                    version, parent_version, published_at,
                    supplement_count, withdrawal_count,
                    recompute_from_finish_time,
                    reused_prefix_entries, recomputed_active_results
                ) VALUES (1, NULL, ?, 0, 0, NULL, 0, 0)
                """,
                (utc_now(),),
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _load_snapshot(
    connection: sqlite3.Connection, version: int
) -> list[Result]:
    rows = connection.execute(
        """
        SELECT result_id, athlete, finish_time, duration_ms
        FROM version_results
        WHERE version = ?
        """,
        (version,),
    ).fetchall()
    return [
        Result(
            result_id=row["result_id"],
            athlete=row["athlete"],
            finish_time=row["finish_time"],
            duration_ms=row["duration_ms"],
        )
        for row in rows
    ]


def _load_progression(
    connection: sqlite3.Connection, version: int
) -> list[ProgressionEntry]:
    rows = connection.execute(
        """
        SELECT seq, kind, result_id, athlete, finish_time, duration_ms
        FROM version_progression
        WHERE version = ?
        ORDER BY seq
        """,
        (version,),
    ).fetchall()
    return [
        ProgressionEntry(
            seq=row["seq"],
            kind=row["kind"],
            result_id=row["result_id"],
            athlete=row["athlete"],
            finish_time=row["finish_time"],
            duration_ms=row["duration_ms"],
        )
        for row in rows
    ]


def _load_operations(
    connection: sqlite3.Connection, version: int
) -> list[OperationSeed]:
    rows = connection.execute(
        """
        SELECT op, result_id, athlete, finish_time, duration_ms
        FROM version_operations
        WHERE version = ?
        ORDER BY seq
        """,
        (version,),
    ).fetchall()
    return [
        OperationSeed(
            op=row["op"],
            result_id=row["result_id"],
            athlete=row["athlete"],
            finish_time=row["finish_time"],
            duration_ms=row["duration_ms"],
        )
        for row in rows
    ]


def latest_version(db_path: str) -> int:
    connection = connect(db_path)
    try:
        row = connection.execute("SELECT MAX(version) AS v FROM versions").fetchone()
        return int(row["v"]) if row["v"] is not None else 0
    finally:
        connection.close()


def publish(db_path: str, announcement: AnnouncementIn) -> int:
    """Apply ``announcement`` and return the newly published version number."""

    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")

        row = connection.execute("SELECT MAX(version) AS v FROM versions").fetchone()
        current = int(row["v"]) if row["v"] is not None else 0
        if current == 0:
            raise AppError(
                503,
                "archive_not_initialised",
                "档案尚未初始化，请稍后重试",
            )

        if announcement.base_version > current:
            raise AppError(
                409,
                "base_version_not_published",
                f"提交所指定的版号 {announcement.base_version} 尚未发布；"
                f"当前最新版为 {current}",
            )
        if announcement.base_version != current:
            raise AppError(
                409,
                "stale_base_version",
                f"提交所基于的版号 {announcement.base_version} 已过期；"
                f"当前最新版为 {current}，请基于当前版重新提交",
            )

        base_results = _load_snapshot(connection, current)
        base_progression = _load_progression(connection, current)
        base_time_by_id = {item.result_id: item.finish_time for item in base_results}

        # Validate supplement ids against the global registry first: an id is
        # never reused, even after its result is withdrawn.  This keeps every
        # historical snapshot unambiguous.
        for supplement in announcement.supplements:
            registry = connection.execute(
                "SELECT status FROM results WHERE result_id = ?",
                (supplement.result_id,),
            ).fetchone()
            if registry is not None and registry["status"] == "active":
                raise AppError(
                    409,
                    "duplicate_result_id",
                    f"成绩编号 {supplement.result_id} 已被有效成绩使用，不能重复补登",
                )
            if registry is not None:
                raise AppError(
                    409,
                    "retired_result_id",
                    f"成绩编号 {supplement.result_id} 曾用于已撤销成绩，不能再次使用",
                )

        # Distinguish unknown ids from already-withdrawn ids for a clear error.
        for withdrawal in announcement.withdrawals:
            registry = connection.execute(
                "SELECT status FROM results WHERE result_id = ?",
                (withdrawal.result_id,),
            ).fetchone()
            if registry is None:
                raise AppError(
                    404,
                    "unknown_result",
                    f"成绩编号 {withdrawal.result_id} 不存在，无法撤销",
                )
            if registry["status"] != "active":
                raise AppError(
                    409,
                    "already_withdrawn",
                    f"成绩编号 {withdrawal.result_id} 已撤销，不能重复撤销",
                )

        try:
            new_results, new_operations = apply_announcement(
                base_results, announcement
            )
        except AppError:
            raise
        except KeyError as exc:
            raise AppError(
                409,
                "result_not_active",
                f"成绩编号 {exc.args[0]} 在版 {current} 中不是有效成绩，无法撤销",
            ) from exc
        except ValueError as exc:
            raise AppError(422, "invalid_announcement", str(exc)) from exc

        # Every changed result's finish time in the new snapshot.  Supplements
        # contribute their new finish time; withdrawals keep the finish time
        # recorded in the base snapshot (they are no longer in new_results).
        new_time_by_id = {item.result_id: item.finish_time for item in new_results}
        changed_times = [
            new_time_by_id[supplement.result_id]
            for supplement in announcement.supplements
        ]
        changed_times.extend(
            base_time_by_id[withdrawal.result_id]
            for withdrawal in announcement.withdrawals
        )
        recompute_from = min(changed_times)

        prefix = [
            entry
            for entry in base_progression
            if entry.finish_time < recompute_from
        ]
        standing = standing_from_prefix(prefix)
        tail_results = [
            item for item in new_results if item.finish_time >= recompute_from
        ]
        tail = compute_from(tail_results, standing=standing, start_seq=len(prefix) + 1)
        progression = prefix + tail

        new_version = current + 1
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
                current,
                utc_now(),
                len(announcement.supplements),
                len(announcement.withdrawals),
                recompute_from,
                len(prefix),
                len(tail_results),
            ),
        )

        connection.executemany(
            """
            INSERT INTO version_results (
                version, result_id, athlete, finish_time, duration_ms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    new_version,
                    item.result_id,
                    item.athlete,
                    item.finish_time,
                    item.duration_ms,
                )
                for item in new_results
            ],
        )

        connection.executemany(
            """
            INSERT INTO version_progression (
                version, seq, kind, result_id, athlete, finish_time, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    new_version,
                    entry.seq,
                    entry.kind,
                    entry.result_id,
                    entry.athlete,
                    entry.finish_time,
                    entry.duration_ms,
                )
                for entry in progression
            ],
        )

        connection.executemany(
            """
            INSERT INTO version_operations (
                version, seq, op, result_id, athlete, finish_time, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    new_version,
                    seq,
                    item.op,
                    item.result_id,
                    item.athlete,
                    item.finish_time,
                    item.duration_ms,
                )
                for seq, item in enumerate(new_operations, start=1)
            ],
        )

        connection.executemany(
            """
            INSERT INTO results (
                result_id, athlete, finish_time, duration_ms,
                status, added_version, withdrawn_version
            ) VALUES (?, ?, ?, ?, 'active', ?, NULL)
            """,
            [
                (
                    supplement.result_id,
                    supplement.athlete,
                    new_time_by_id[supplement.result_id],
                    supplement.duration_ms,
                    new_version,
                )
                for supplement in announcement.supplements
            ],
        )

        for withdrawal in announcement.withdrawals:
            cursor = connection.execute(
                """
                UPDATE results
                SET status = 'withdrawn', withdrawn_version = ?
                WHERE result_id = ? AND status = 'active'
                """,
                (new_version, withdrawal.result_id),
            )
            if cursor.rowcount != 1:
                raise AppError(
                    409,
                    "result_not_active",
                    f"成绩编号 {withdrawal.result_id} 已不是有效成绩，无法撤销",
                )

        connection.execute("COMMIT")
        return new_version
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def list_versions(db_path: str, offset: int = 0, limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT v.version, v.parent_version, v.published_at,
                   v.supplement_count, v.withdrawal_count,
                   (SELECT COUNT(*) FROM version_progression p
                    WHERE p.version = v.version) AS progression_entry_count,
                   (SELECT COUNT(*) FROM version_results r
                    WHERE r.version = v.version) AS active_result_count
            FROM versions v
            ORDER BY v.version ASC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def get_version(db_path: str, version: int) -> dict:
    connection = connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT version, parent_version, published_at,
                   supplement_count, withdrawal_count, recompute_from_finish_time,
                   reused_prefix_entries, recomputed_active_results
            FROM versions WHERE version = ?
            """,
            (version,),
        ).fetchone()
        if row is None:
            raise AppError(404, "unknown_version", f"版号 {version} 不存在")

        progression = _load_progression(connection, version)
        active_results = sorted(
            _load_snapshot(connection, version),
            key=lambda item: (item.finish_time, item.result_id),
        )
        operations = _load_operations(connection, version)

        detail = dict(row)
        detail.update(
            {
                "progression_entry_count": len(progression),
                "active_result_count": len(active_results),
                "progression": [
                    {
                        "seq": entry.seq,
                        "kind": entry.kind,
                        "result_id": entry.result_id,
                        "athlete": entry.athlete,
                        "finish_time": entry.finish_time,
                        "duration_ms": entry.duration_ms,
                    }
                    for entry in progression
                ],
                "active_results": [
                    {
                        "result_id": item.result_id,
                        "athlete": item.athlete,
                        "finish_time": item.finish_time,
                        "duration_ms": item.duration_ms,
                    }
                    for item in active_results
                ],
                "operations": [
                    {
                        "seq": seq,
                        "op": item.op,
                        "result_id": item.result_id,
                        "athlete": item.athlete,
                        "finish_time": item.finish_time,
                        "duration_ms": item.duration_ms,
                    }
                    for seq, item in enumerate(operations, start=1)
                ],
            }
        )
        return detail
    finally:
        connection.close()


def get_progression(db_path: str, version: int) -> list[ProgressionEntry]:
    connection = connect(db_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM versions WHERE version = ?", (version,)
        ).fetchone()
        if exists is None:
            raise AppError(404, "unknown_version", f"版号 {version} 不存在")
        return _load_progression(connection, version)
    finally:
        connection.close()


def list_results(
    db_path: str, status: Literal["active", "withdrawn", "all"] = "active"
) -> list[dict]:
    connection = connect(db_path)
    try:
        if status == "all":
            rows = connection.execute(
                """
                SELECT result_id, athlete, finish_time, duration_ms, status,
                       added_version, withdrawn_version
                FROM results
                ORDER BY finish_time, result_id
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT result_id, athlete, finish_time, duration_ms, status,
                       added_version, withdrawn_version
                FROM results
                WHERE status = ?
                ORDER BY finish_time, result_id
                """,
                (status,),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()
