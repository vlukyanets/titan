"""Time block placement: working hours, buffers and no overlaps."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from titan.domains.calendar.planner import Hours, free_slots, place

KYIV = ZoneInfo("Europe/Kyiv")
MONDAY = date(2026, 10, 5)
HOURS = Hours(KYIV, time(9), time(17), [1, 2, 3, 4, 5], timedelta(minutes=10))
EARLY = datetime(2026, 10, 5, 6, 0, tzinfo=KYIV)


def at(hour: int, minute: int = 0, day: date = MONDAY) -> datetime:
    return datetime.combine(day, time(hour, minute), KYIV)


def test_free_slots_keep_the_buffer_around_events_and_now() -> None:
    busy = [(at(10), at(11)), (at(11, 5), at(12)), (at(16, 55), at(18))]
    assert free_slots(MONDAY, HOURS, busy, EARLY) == [
        (at(9), at(9, 50)),
        (at(12, 10), at(16, 45)),
    ]
    # Later in the day the slots start a buffer after now, at whole five minutes.
    assert free_slots(MONDAY, HOURS, [], at(13, 3))[0][0] == at(13, 15)
    assert free_slots(MONDAY, HOURS, [], at(13, 5))[0][0] == at(13, 15)
    assert free_slots(date(2026, 10, 10), HOURS, [], EARLY) == []


def test_place_fits_in_order_with_buffers_between_blocks() -> None:
    slots = [(at(9), at(9, 50)), (at(12, 10), at(16, 45))]
    hour, half = timedelta(hours=1), timedelta(minutes=30)
    placed = place([hour, half, timedelta(hours=5), half], slots, HOURS.buffer)
    assert placed == [
        (at(12, 10), at(13, 10)),
        (at(9), at(9, 30)),
        None,
        (at(13, 20), at(13, 50)),
    ]
    # Nothing placed overlaps anything busy or another block.
    blocks = sorted(p for p in placed if p)
    assert all(a[1] + HOURS.buffer <= b[0] for a, b in pairwise(blocks))
