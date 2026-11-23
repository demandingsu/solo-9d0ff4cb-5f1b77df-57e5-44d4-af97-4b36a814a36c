"""Validate and normalise one announcement against its base version.

The functions here are pure: they take the base version's active results and
an announcement, and return the next version's active results plus the
operations contributed by this announcement.  All database access happens in
:mod:`app.service`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.progression import Result
from app.schemas import AnnouncementIn
from app.timeutil import canonical_finish_time


@dataclass(frozen=True)
class OperationSeed:
    """One applied operation, ready to be stored."""

    op: str
    result_id: str
    athlete: str | None = None
    finish_time: str | None = None
    duration_ms: int | None = None


def validate_announcement(announcement: AnnouncementIn) -> None:
    """Reject malformed announcements that pydantic cannot detect itself."""

    supplement_ids = [item.result_id for item in announcement.supplements]
    withdrawal_ids = [item.result_id for item in announcement.withdrawals]

    seen: set[str] = set()
    for result_id in supplement_ids:
        if result_id in seen:
            raise _ValidationError(f"同一公告中补登成绩编号重复: {result_id}")
        seen.add(result_id)

    seen.clear()
    for result_id in withdrawal_ids:
        if result_id in seen:
            raise _ValidationError(f"同一公告中撤销成绩编号重复: {result_id}")
        seen.add(result_id)

    overlap = set(supplement_ids) & set(withdrawal_ids)
    if overlap:
        first = sorted(overlap)[0]
        raise _ValidationError(f"同一公告不能同时补登并撤销成绩: {first}")


class _ValidationError(ValueError):
    pass


def apply_announcement(
    base_results: list[Result],
    announcement: AnnouncementIn,
) -> tuple[list[Result], list[OperationSeed]]:
    """Apply an announcement to a base version.

    Returns ``(new_active_results, operations)`` where ``operations`` contains
    only the operations carried by this announcement, in announcement order.
    """

    validate_announcement(announcement)

    active: dict[str, Result] = {item.result_id: item for item in base_results}
    operations: list[OperationSeed] = []

    for supplement in announcement.supplements:
        if supplement.result_id in active:
            raise _ValidationError(
                f"成绩编号已存在且仍有效，不能重复补登: {supplement.result_id}"
            )
        result = Result(
            result_id=supplement.result_id,
            athlete=supplement.athlete,
            finish_time=canonical_finish_time(supplement.finish_time),
            duration_ms=supplement.duration_ms,
        )
        active[result.result_id] = result
        operations.append(
            OperationSeed(
                op="supplement",
                result_id=result.result_id,
                athlete=result.athlete,
                finish_time=result.finish_time,
                duration_ms=result.duration_ms,
            )
        )

    for withdrawal in announcement.withdrawals:
        result = active.pop(withdrawal.result_id, None)
        if result is None:
            # Unknown ids and ids that are already withdrawn are rejected by
            # the service, which can distinguish the two cases using the
            # global registry.
            raise KeyError(withdrawal.result_id)
        operations.append(
            OperationSeed(
                op="withdraw",
                result_id=result.result_id,
                athlete=result.athlete,
                finish_time=result.finish_time,
                duration_ms=result.duration_ms,
            )
        )

    return list(active.values()), operations
