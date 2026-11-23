"""Time helpers.

All finish times are stored as canonical UTC ISO-8601 strings with
microseconds.  That representation is lexicographically sortable, which lets
SQLite order finish times directly and makes the stored snapshots independent
of the server's local timezone.
"""

from __future__ import annotations

from datetime import datetime, timezone


def canonical_finish_time(value: datetime) -> str:
    """Return a canonical UTC finish time.

    Naive datetimes are interpreted as UTC.  Explicit offsets are converted to
    UTC so that ``finish_time`` ordering is always chronological.
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def utc_now() -> str:
    """Return the current UTC time in the same canonical representation."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
