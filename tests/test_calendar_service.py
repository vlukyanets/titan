"""Events in their own time zone, occurrences, busy intervals and preferences."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService
from titan.domains.calendar.errors import ForbiddenError, InvalidEventError, NotFoundError
from titan.domains.calendar.service import CalendarService, all_day_bounds, merge
from titan.domains.tasks.service import TasksService

KYIV = ZoneInfo("Europe/Kyiv")
# Kyiv leaves summer time on Sunday 25 October 2026: UTC+3 before, UTC+2 after.
MONDAY_BEFORE = datetime(2026, 10, 19, 9, 0, tzinfo=KYIV)


def test_all_day_bounds_are_local_midnights() -> None:
    begins, ends = all_day_bounds(
        datetime(2026, 10, 24, 15, 0, tzinfo=KYIV), datetime(2026, 10, 25, 10, 0, tzinfo=KYIV), KYIV
    )
    assert begins == datetime(2026, 10, 23, 21, 0, tzinfo=UTC)
    # Two local days across the change: 49 hours.
    assert ends == datetime(2026, 10, 25, 22, 0, tzinfo=UTC)
    same = datetime(2026, 10, 24, tzinfo=KYIV)
    assert all_day_bounds(same, same, KYIV)[1] - all_day_bounds(same, same, KYIV)[0] == timedelta(
        hours=24
    )


def test_merge_joins_overlapping_and_touching_intervals() -> None:
    t = datetime(2026, 10, 1, tzinfo=UTC)
    h = timedelta(hours=1)
    assert merge(
        [(t + 3 * h, t + 4 * h), (t, t + h), (t + h, t + 2 * h), (t + 3 * h, t + 5 * h)]
    ) == [
        (t, t + 2 * h),
        (t + 3 * h, t + 5 * h),
    ]


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def user(sessions: async_sessionmaker[AsyncSession], username: str) -> uuid.UUID:
    async with sessions() as session:
        created = await AccountsService(session).create_user(
            username, "a-long-test-password", role=Role.MEMBER
        )
        return created.id


@pytest.mark.db
async def test_a_weekly_event_keeps_its_local_time_across_the_change(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        calendar = CalendarService(session)
        await calendar.set_prefs(
            anna,
            time_zone="Europe/Kyiv",
            work_start=time(9),
            work_end=time(18),
            work_days=[1, 2, 3, 4, 5],
            buffer_minutes=15,
        )
        standup = await calendar.create_event(
            anna,
            "Stand-up",
            MONDAY_BEFORE,
            MONDAY_BEFORE + timedelta(minutes=15),
            recurrence="FREQ=WEEKLY;BYDAY=MO",
        )
        assert standup.event.time_zone == "Europe/Kyiv"
        found = await calendar.occurrences(
            anna, datetime(2026, 10, 18, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC)
        )
        assert [o.starts_at for o in found] == [
            datetime(2026, 10, 19, 6, 0, tzinfo=UTC),
            datetime(2026, 10, 26, 7, 0, tzinfo=UTC),
            datetime(2026, 11, 2, 7, 0, tzinfo=UTC),
        ]
        assert all(o.ends_at - o.starts_at == timedelta(minutes=15) for o in found)

        weekend = await calendar.create_event(
            anna,
            "Hiking",
            datetime(2026, 10, 24, 12, tzinfo=KYIV),
            datetime(2026, 10, 25, 12, tzinfo=KYIV),
            all_day=True,
            recurrence="FREQ=WEEKLY;UNTIL=20261102T000000Z",
        )
        hikes = [
            o
            for o in await calendar.occurrences(
                anna, datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 11, 20, tzinfo=UTC)
            )
            if o.event.id == weekend.event.id
        ]
        assert [(o.starts_at, o.ends_at) for o in hikes] == [
            (datetime(2026, 10, 23, 21, tzinfo=UTC), datetime(2026, 10, 25, 22, tzinfo=UTC)),
            (datetime(2026, 10, 30, 22, tzinfo=UTC), datetime(2026, 11, 1, 22, tzinfo=UTC)),
        ]


@pytest.mark.db
async def test_attendees_see_events_and_only_owners_change_them(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    boris = await user(sessions, "boris")
    carla = await user(sessions, "carla")
    start = datetime(2026, 10, 1, 17, tzinfo=UTC)
    async with sessions() as session:
        calendar = CalendarService(session)
        dinner = await calendar.create_event(
            anna,
            "Family dinner",
            start,
            start + timedelta(hours=2),
            attendees=[boris, anna, boris],
            location="Home",
        )
        assert dinner.attendees == [boris]
        assert dinner.event.time_zone == "UTC"  # Anna has no preferences yet
        window = (start - timedelta(days=1), start + timedelta(days=1))
        assert [o.event.id for o in await calendar.occurrences(boris, *window)] == [dinner.event.id]
        assert await calendar.occurrences(carla, *window) == []
        with pytest.raises(NotFoundError):
            await calendar.get_event(carla, dinner.event.id)
        with pytest.raises(ForbiddenError):
            await calendar.update_event(boris, dinner.event.id, {"title": "Pizza"})
        with pytest.raises(ForbiddenError):
            await calendar.delete_event(boris, dinner.event.id)

        # Boris's own overlapping call merges into one busy interval.
        await calendar.create_event(
            boris, "Call", start + timedelta(hours=1), start + timedelta(hours=3)
        )
        assert await calendar.busy(boris, *window) == [(start, start + timedelta(hours=3))]

        moved = await calendar.update_event(
            anna,
            dinner.event.id,
            {"starts_at": start + timedelta(days=1), "ends_at": start + timedelta(days=1, hours=2)},
        )
        assert moved.event.starts_at == start + timedelta(days=1)
        assert moved.attendees == [boris]
        with pytest.raises(InvalidEventError, match="ends after"):
            await calendar.update_event(anna, dinner.event.id, {"ends_at": start})
        assert (
            await calendar.get_event(anna, dinner.event.id)
        ).event.starts_at == moved.event.starts_at
        await calendar.update_event(anna, dinner.event.id, {"attendees": []})
        with pytest.raises(NotFoundError):
            await calendar.get_event(boris, dinner.event.id)
        await calendar.delete_event(anna, dinner.event.id)


@pytest.mark.db
async def test_invalid_events_and_windows_are_refused(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    start = datetime(2026, 10, 1, 9, tzinfo=UTC)
    async with sessions() as session:
        calendar = CalendarService(session)
        task = await TasksService(session).create_task(anna, "Write the report")
        block = await calendar.create_event(
            anna, "Report", start, start + timedelta(hours=2), task_id=task.id
        )
        assert block.event.task_id == task.id
        for bad in (
            {"title": " "},
            {"ends_at": start},
            {"ends_at": start + timedelta(days=32)},
            {"time_zone": "Mars/Olympus"},
            {"recurrence": "FREQ=MINUTELY"},
            {"attendees": [uuid.uuid4()]},
            {"task_id": uuid.uuid4()},
            {"starts_at": datetime(2026, 10, 1, 9)},
        ):
            args: dict[str, object] = {
                "title": "x",
                "starts_at": start,
                "ends_at": start + timedelta(hours=1),
            } | bad
            with pytest.raises(InvalidEventError):
                await calendar.create_event(anna, **args)  # type: ignore[arg-type]
        with pytest.raises(InvalidEventError, match="92 days"):
            await calendar.occurrences(anna, start, start + timedelta(days=93))
        with pytest.raises(InvalidEventError):
            await calendar.set_prefs(
                anna,
                time_zone="UTC",
                work_start=time(18),
                work_end=time(9),
                work_days=[1],
                buffer_minutes=0,
            )
        prefs = await calendar.prefs(anna)
        assert (prefs.time_zone, prefs.work_days, prefs.buffer_minutes) == (
            "UTC",
            [1, 2, 3, 4, 5],
            10,
        )
