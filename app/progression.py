"""Record-progression computation.

The rules are deliberate and small:

* results are ordered by finish time, then by result id (string order);
* the first result in that order is the standing record ("first");
* each later result is a "refresh" if it is faster than the standing record,
  and a "tie" if it is exactly as fast as the standing record;
* slower results are not part of the progression;
* a tie does not replace the standing record; it is listed, but the athlete
  who first achieved the time remains the record holder.

Because results are ordered by finish time rather than duration, ties can only
occur when a later run has exactly the same duration as the standing record.
"""

from __future__ import annotations

from typing import Iterable, NamedTuple, Sequence


class Result(NamedTuple):
    result_id: str
    athlete: str
    finish_time: str
    duration_ms: int


class ProgressionEntry(NamedTuple):
    seq: int
    kind: str
    result_id: str
    athlete: str
    finish_time: str
    duration_ms: int


def order_results(results: Iterable[Result]) -> list[Result]:
    """Return results in the canonical progression order."""

    return sorted(results, key=lambda item: (item.finish_time, item.result_id))


def _classify(standing: Result | None, result: Result) -> str | None:
    if standing is None:
        return "first"
    if result.duration_ms < standing.duration_ms:
        return "refresh"
    if result.duration_ms == standing.duration_ms:
        return "tie"
    return None


def compute_from(
    results: Iterable[Result],
    standing: Result | None,
    start_seq: int = 1,
) -> list[ProgressionEntry]:
    """Compute progression entries for ``results`` given a standing record.

    ``results`` need not be sorted; they are ordered canonically first.
    """

    entries: list[ProgressionEntry] = []
    seq = start_seq
    for result in order_results(results):
        kind = _classify(standing, result)
        if kind is None:
            continue
        entries.append(
            ProgressionEntry(
                seq=seq,
                kind=kind,
                result_id=result.result_id,
                athlete=result.athlete,
                finish_time=result.finish_time,
                duration_ms=result.duration_ms,
            )
        )
        seq += 1
        if kind in ("first", "refresh"):
            standing = result
    return entries


def compute_progression(results: Iterable[Result]) -> list[ProgressionEntry]:
    """Compute a complete progression from scratch."""

    return compute_from(results, standing=None, start_seq=1)


def standing_from_prefix(
    prefix: Sequence[ProgressionEntry],
) -> Result | None:
    """Return the record in force after a copied prefix of a progression."""

    standing: Result | None = None
    for entry in prefix:
        if entry.kind in ("first", "refresh"):
            standing = Result(
                result_id=entry.result_id,
                athlete=entry.athlete,
                finish_time=entry.finish_time,
                duration_ms=entry.duration_ms,
            )
    return standing
