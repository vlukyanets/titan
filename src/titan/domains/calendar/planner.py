"""Placing time blocks for tasks (docs/spec/domains/calendar.md#planning-behaviour).

Pure functions: the caller gives the busy intervals and the preferences, so the
rules (working hours, buffers, no overlaps) are tested without a database.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

Interval = tuple[datetime, datetime]


@dataclass(frozen=True)
class Hours:
    """A user's working hours, in their zone."""

    zone: ZoneInfo
    start: time
    end: time
    # ISO weekdays, 1 = Monday.
    days: Sequence[int]
    buffer: timedelta


def _round_up(value: datetime) -> datetime:
    """To the next whole five minutes, so blocks start at times people use."""
    step = timedelta(minutes=5)
    floor = value - timedelta(
        minutes=value.minute % 5, seconds=value.second, microseconds=value.microsecond
    )
    return floor if floor == value else floor + step


def working_window(day: date, hours: Hours) -> Interval | None:
    if day.isoweekday() not in hours.days:
        return None
    return (
        datetime.combine(day, hours.start, hours.zone),
        datetime.combine(day, hours.end, hours.zone),
    )


def free_slots(day: date, hours: Hours, busy: Sequence[Interval], now: datetime) -> list[Interval]:
    """The gaps of the working day that keep a buffer from everything busy and from now."""
    window = working_window(day, hours)
    if window is None:
        return []
    start, end = max(window[0], _round_up(now + hours.buffer)), window[1]
    slots: list[Interval] = []
    for begins, ends in sorted(busy):
        if begins - hours.buffer > start:
            slots.append((start, min(begins - hours.buffer, end)))
        start = max(start, ends + hours.buffer)
        if start >= end:
            break
    if start < end:
        slots.append((start, end))
    return [(a, b) for a, b in slots if b > a]


def place(
    durations: Sequence[timedelta], slots: Sequence[Interval], buffer: timedelta
) -> list[Interval | None]:
    """First fit, in the given order; a block that fits nowhere gets None.

    Blocks placed in the same slot keep the buffer between them.
    """
    free = list(slots)
    placed: list[Interval | None] = []
    for length in durations:
        for i, (begins, ends) in enumerate(free):
            if ends - begins >= length:
                placed.append((begins, begins + length))
                free[i] = (begins + length + buffer, ends)
                break
        else:
            placed.append(None)
    return placed
