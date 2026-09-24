"""Trackers: templates, validation, entries, stats per local period and streaks."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService
from titan.domains.calendar.service import CalendarService
from titan.domains.trackers.errors import (
    ArchivedError,
    DuplicateNameError,
    InvalidEntryError,
    InvalidTrackerError,
    NotFoundError,
)
from titan.domains.trackers.models import Direction, Period, Tracker, TrackerKind
from titan.domains.trackers.service import (
    Target,
    TrackersService,
    is_met,
    next_period,
    periods,
    shift,
)

KYIV = ZoneInfo("Europe/Kyiv")
# A Thursday; Kyiv is on summer time (UTC+3) until 25 October 2026.
THURSDAY = date(2026, 10, 15)


def at(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), KYIV)


def test_period_arithmetic() -> None:
    assert periods(date(2026, 10, 14), date(2026, 10, 27), Period.WEEK) == [
        date(2026, 10, 12),
        date(2026, 10, 19),
        date(2026, 10, 26),
    ]
    assert next_period(date(2026, 12, 1), Period.MONTH) == date(2027, 1, 1)
    assert shift(date(2026, 2, 1), Period.MONTH, 3) == date(2025, 11, 1)
    assert shift(date(2026, 10, 12), Period.WEEK, 2) == date(2026, 9, 28)


def test_targets_are_met_in_their_direction() -> None:
    at_least = Target(Decimal(8), Period.DAY, Direction.AT_LEAST)
    at_most = Target(Decimal(300), Period.WEEK, Direction.AT_MOST)
    assert is_met(Decimal(8), 3, at_least)
    assert not is_met(Decimal("7.5"), 3, at_least)
    assert is_met(Decimal(0), 0, at_most)
    assert not is_met(Decimal("300.01"), 4, at_most)
    assert not is_met(Decimal(0), 0, None)
    assert is_met(Decimal(0), 1, None)


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def user(
    sessions: async_sessionmaker[AsyncSession], username: str, zone: str | None = "Europe/Kyiv"
) -> uuid.UUID:
    async with sessions() as session:
        created = await AccountsService(session).create_user(
            username, "a-long-test-password", role=Role.MEMBER
        )
        if zone is not None:
            await CalendarService(session).set_prefs(
                created.id,
                time_zone=zone,
                work_start=time(9),
                work_end=time(18),
                work_days=[1, 2, 3, 4, 5],
                buffer_minutes=15,
            )
        return created.id


async def created_on(session: AsyncSession, tracker: Tracker, day: date) -> None:
    await session.execute(
        update(Tracker).where(Tracker.id == tracker.id).values(created_at=at(day, 8))
    )
    await session.commit()
    await session.refresh(tracker)


@pytest.mark.db
async def test_templates_fill_in_kind_unit_and_bounds(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        trackers = TrackersService(session)
        mood = await trackers.create_tracker(anna, "  Настрій ", template="mood")
        assert (mood.name, mood.kind, mood.unit) == ("Настрій", TrackerKind.HEALTH, "score")
        assert (mood.min_value, mood.max_value) == (Decimal(1), Decimal(5))

        weight = await trackers.create_tracker(anna, "Weight", template="weight", unit="lb")
        assert weight.unit == "lb"
        assert weight.min_value == Decimal(0)

        with pytest.raises(InvalidTrackerError, match="unit"):
            await trackers.create_tracker(anna, "Траты", template="expense")
        spending = await trackers.create_tracker(
            anna,
            "Траты",
            template="expense",
            unit="UAH",
            target={"value": "3000", "period": "week", "direction": "at_most"},
        )
        assert (spending.kind, spending.target_period) == (TrackerKind.FINANCE, Period.WEEK)
        assert spending.target_direction is Direction.AT_MOST

        with pytest.raises(InvalidTrackerError, match="template"):
            await trackers.create_tracker(anna, "Steps", template="steps")
        with pytest.raises(InvalidTrackerError, match="kind"):
            await trackers.create_tracker(anna, "Steps", unit="count")
        custom = await trackers.create_tracker(anna, "Steps", kind="custom", unit="count")
        assert custom.min_value is None


@pytest.mark.db
async def test_names_are_unique_per_owner_ignoring_case(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    boris = await user(sessions, "boris")
    async with sessions() as session:
        trackers = TrackersService(session)
        steps = await trackers.create_tracker(anna, "Шаги", template="habit")
        with pytest.raises(DuplicateNameError):
            await trackers.create_tracker(anna, "шаги", template="habit")
        # Another user may use the same name.
        await trackers.create_tracker(boris, "Шаги", template="habit")
        sleep = await trackers.create_tracker(anna, "Sleep", template="sleep")
        with pytest.raises(DuplicateNameError):
            await trackers.update_tracker(anna, sleep.id, {"name": "ШАГИ"})
        # Renaming to a different case of its own name is fine.
        renamed = await trackers.update_tracker(anna, steps.id, {"name": "ШАГИ"})
        assert renamed.name == "ШАГИ"
        assert [t.name for t in await trackers.trackers(anna)] == ["Sleep", "ШАГИ"]


@pytest.mark.db
async def test_cross_field_rules_and_refused_updates_change_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        trackers = TrackersService(session)
        with pytest.raises(InvalidTrackerError, match="min_value"):
            await trackers.create_tracker(
                anna, "Odd", kind="custom", unit="x", min_value=5, max_value=1
            )
        with pytest.raises(InvalidTrackerError, match="schedule"):
            await trackers.create_tracker(
                anna,
                "Gym",
                template="workout",
                schedule="FREQ=WEEKLY;BYDAY=MO,WE,FR",
                target={"value": 120, "period": "week"},
            )
        with pytest.raises(InvalidTrackerError, match="schedule"):
            await trackers.create_tracker(anna, "Gym", template="workout", schedule="FREQ=HOURLY")
        gym = await trackers.create_tracker(
            anna, "Gym", template="workout", schedule="rrule:freq=weekly;byday=mo,we,fr"
        )
        assert gym.schedule == "FREQ=WEEKLY;BYDAY=MO,WE,FR"

        with pytest.raises(InvalidTrackerError, match="schedule"):
            await trackers.update_tracker(
                anna, gym.id, {"name": "Gym!", "target": {"value": 3, "period": "week"}}
            )
        with pytest.raises(InvalidTrackerError, match="no field"):
            await trackers.update_tracker(anna, gym.id, {"owner_id": uuid.uuid4()})
        await session.refresh(gym)
        assert (gym.name, gym.target_value) == ("Gym", None)

        # Dropping the schedule makes room for the weekly target.
        changed = await trackers.update_tracker(
            anna,
            gym.id,
            {"schedule": None, "target": {"value": 150, "period": "week"}},
        )
        assert (changed.schedule, changed.target_value) == (None, Decimal(150))
        assert changed.target_direction is Direction.AT_LEAST


@pytest.mark.db
async def test_trackers_are_private_to_their_owner(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    boris = await user(sessions, "boris")
    async with sessions() as session:
        trackers = TrackersService(session)
        spending = await trackers.create_tracker(anna, "Spending", template="expense", unit="EUR")
        entry = await trackers.log(anna, spending.id, "23.40", category="Groceries")
        for call in (
            trackers.get_tracker(boris, spending.id),
            trackers.update_tracker(boris, spending.id, {"name": "Mine"}),
            trackers.delete_tracker(boris, spending.id),
            trackers.log(boris, spending.id, 1),
            trackers.entries(boris, spending.id),
            trackers.update_entry(boris, spending.id, entry.id, {"value": 1}),
            trackers.delete_entry(boris, spending.id, entry.id),
            trackers.stats(boris, spending.id),
        ):
            with pytest.raises(NotFoundError):
                await call
        assert await trackers.trackers(boris) == []


@pytest.mark.db
async def test_entries_are_exact_bounded_and_listed_newest_first(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        trackers = TrackersService(session)
        mood = await trackers.create_tracker(anna, "Mood", template="mood")
        with pytest.raises(InvalidEntryError, match="above 5"):
            await trackers.log(anna, mood.id, 6)
        with pytest.raises(InvalidEntryError, match="not a number"):
            await trackers.log(anna, mood.id, True)
        with pytest.raises(InvalidEntryError, match="not a number"):
            await trackers.log(anna, mood.id, "NaN")
        with pytest.raises(InvalidEntryError, match="time zone"):
            await trackers.log(anna, mood.id, 3, at=datetime(2026, 10, 15, 12))

        coffee = await trackers.create_tracker(anna, "Coffee", template="expense", unit="EUR")
        noon = at(THURSDAY)
        first = await trackers.log(anna, coffee.id, 0.1, at=noon, category=" Cafe ")
        second = await trackers.log(anna, coffee.id, 0.2, at=noon)
        third = await trackers.log(anna, coffee.id, "1.23456", at=noon - timedelta(hours=1))
        assert first.category == "cafe"
        assert third.value == Decimal("1.2346")

        # Equal times fall back to the id, so paging never skips or repeats.
        page = await trackers.entries(anna, coffee.id, limit=2)
        assert [e.id for e in page] == [second.id, first.id]
        rest = await trackers.entries(anna, coffee.id, before=page[-1].id)
        assert [e.id for e in rest] == [third.id]
        assert [e.id for e in await trackers.entries(anna, coffee.id, category="CAFE")] == [
            first.id
        ]
        assert await trackers.entries(anna, coffee.id, start=noon, end=noon) == []

        stats = await trackers.stats(
            anna, coffee.id, start=THURSDAY, end=THURSDAY, now=noon + timedelta(hours=1)
        )
        assert stats.sum == Decimal("1.5346")

        moved = await trackers.update_entry(
            anna, coffee.id, third.id, {"value": 2, "category": "Books", "note": " novel "}
        )
        assert (moved.value, moved.category, moved.note) == (Decimal(2), "books", "novel")
        await trackers.delete_entry(anna, coffee.id, second.id)
        assert len(await trackers.entries(anna, coffee.id)) == 2


@pytest.mark.db
async def test_archived_trackers_take_no_entries_and_deleting_removes_them(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        trackers = TrackersService(session)
        water = await trackers.create_tracker(anna, "Water", template="habit")
        await trackers.log(anna, water.id, 1)
        await trackers.update_tracker(anna, water.id, {"archived": True})
        with pytest.raises(ArchivedError):
            await trackers.log(anna, water.id, 1)
        assert await trackers.trackers(anna) == []
        assert [t.id for t in await trackers.trackers(anna, archived=True)] == [water.id]
        assert [t.id for t in await trackers.trackers(anna, archived=None)] == [water.id]

        await trackers.delete_tracker(anna, water.id)
        with pytest.raises(NotFoundError):
            await trackers.get_tracker(anna, water.id)


@pytest.mark.db
async def test_stats_group_by_local_days_weeks_and_months(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        trackers = TrackersService(session)
        spending = await trackers.create_tracker(anna, "Spending", template="expense", unit="EUR")
        # 00:30 on Friday in Kyiv is still Thursday in UTC: it belongs to Friday.
        await trackers.log(anna, spending.id, 10, at=at(THURSDAY + timedelta(days=1), 0, 30))
        await trackers.log(anna, spending.id, 5, at=at(THURSDAY, 23, 50), category="food")
        await trackers.log(anna, spending.id, 20, at=at(THURSDAY, 9), category="food")
        # Across the change to winter time on 25 October.
        await trackers.log(anna, spending.id, 7, at=at(date(2026, 10, 26), 0, 10))
        now = at(date(2026, 10, 28))

        days = await trackers.stats(
            anna, spending.id, start=THURSDAY, end=date(2026, 10, 18), now=now
        )
        assert days.time_zone == "Europe/Kyiv"
        assert [(b.start.day, b.count, b.sum) for b in days.buckets] == [
            (15, 2, Decimal(25)),
            (16, 1, Decimal(10)),
            (17, 0, Decimal(0)),
            (18, 0, Decimal(0)),
        ]
        assert days.buckets[0].average == Decimal("12.5")
        assert (days.buckets[0].min, days.buckets[0].max) == (Decimal(5), Decimal(20))
        assert days.buckets[2].average is None
        # No target: a day counts as met when it has an entry.
        assert [b.met for b in days.buckets] == [True, True, False, False]
        assert (days.count, days.sum, days.per_period) == (3, Decimal(35), Decimal("8.75"))
        assert [(c.category, c.sum) for c in days.categories] == [
            ("food", Decimal(25)),
            (None, Decimal(10)),
        ]

        weeks = await trackers.stats(
            anna, spending.id, period=Period.WEEK, start=THURSDAY, end=date(2026, 10, 26), now=now
        )
        assert (weeks.start, weeks.end) == (date(2026, 10, 12), date(2026, 11, 1))
        assert [(b.start, b.sum) for b in weeks.buckets] == [
            (date(2026, 10, 12), Decimal(35)),
            (date(2026, 10, 19), Decimal(0)),
            (date(2026, 10, 26), Decimal(7)),
        ]

        months = await trackers.stats(anna, spending.id, period=Period.MONTH, now=now)
        # The last 12 months by default, ending with the current one.
        assert (months.start, months.end) == (date(2025, 11, 1), date(2026, 10, 31))
        assert months.buckets[-1].sum == Decimal(42)

        default = await trackers.stats(anna, spending.id, now=now)
        assert (default.start, default.end) == (date(2026, 9, 29), date(2026, 10, 28))

        with pytest.raises(InvalidTrackerError, match="after"):
            await trackers.stats(anna, spending.id, start=THURSDAY, end=date(2026, 10, 1))
        with pytest.raises(InvalidTrackerError, match="400"):
            await trackers.stats(anna, spending.id, start=date(2024, 1, 1), end=THURSDAY)


@pytest.mark.db
async def test_a_daily_habit_streak_survives_an_open_today(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        trackers = TrackersService(session)
        read = await trackers.create_tracker(anna, "Reading", template="habit")
        await created_on(session, read, date(2026, 10, 1))
        for day in (8, 10, 11, 12, 13, 14):
            await trackers.log(anna, read.id, 1, at=at(date(2026, 10, day), 22))
        # Thursday has no entry yet: it does not break the streak.
        assert await trackers.streak(anna, read.id, now=at(THURSDAY, 9)) == (5, Period.DAY)
        await trackers.log(anna, read.id, 1, at=at(THURSDAY, 8))
        assert await trackers.streak(anna, read.id, now=at(THURSDAY, 9)) == (6, Period.DAY)
        # Friday passes without an entry: on Saturday the streak is gone.
        assert await trackers.streak(anna, read.id, now=at(date(2026, 10, 17), 9)) == (
            0,
            Period.DAY,
        )


@pytest.mark.db
async def test_a_scheduled_habit_counts_only_its_days(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        trackers = TrackersService(session)
        gym = await trackers.create_tracker(
            anna, "Gym", template="workout", schedule="FREQ=WEEKLY;BYDAY=MO,WE,FR"
        )
        await created_on(session, gym, date(2026, 10, 5))
        # Monday, Wednesday, Friday, Monday, Wednesday; a Sunday walk does not count.
        for day in (5, 7, 9, 11, 12, 14):
            await trackers.log(anna, gym.id, 45, at=at(date(2026, 10, day), 18))
        assert (await trackers.streak(anna, gym.id, now=at(THURSDAY)))[0] == 5
        stats = await trackers.stats(anna, gym.id, start=THURSDAY, end=THURSDAY, now=at(THURSDAY))
        assert stats.streak == 5


@pytest.mark.db
async def test_a_weekly_spending_cap_streak_starts_when_the_tracker_does(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        trackers = TrackersService(session)
        cap = await trackers.create_tracker(
            anna,
            "Eating out",
            template="expense",
            unit="EUR",
            target={"value": 50, "period": "week", "direction": "at_most"},
        )
        await created_on(session, cap, date(2026, 9, 23))
        await trackers.log(anna, cap.id, 60, at=at(date(2026, 9, 30)))
        await trackers.log(anna, cap.id, 30, at=at(date(2026, 10, 6)))
        await trackers.log(anna, cap.id, 15, at=at(date(2026, 10, 8)))
        # Weeks of 5 and 12 October stay within the cap; 28 September's did not.
        assert await trackers.streak(anna, cap.id, now=at(THURSDAY)) == (2, Period.WEEK)
        stats = await trackers.stats(anna, cap.id, now=at(THURSDAY))
        assert stats.period is Period.WEEK
        assert [b.met for b in stats.buckets[-3:]] == [False, True, True]
        # Buckets of another length than the target's are not judged.
        days = await trackers.stats(anna, cap.id, period=Period.DAY, now=at(THURSDAY))
        assert {b.met for b in days.buckets} == {None}

        fresh = await trackers.create_tracker(
            anna,
            "Taxi",
            template="expense",
            unit="EUR",
            target={"value": 20, "period": "week", "direction": "at_most"},
        )
        await created_on(session, fresh, THURSDAY)
        # Only the current week counts, not the years before the tracker existed.
        assert await trackers.streak(anna, fresh.id, now=at(THURSDAY)) == (1, Period.WEEK)


@pytest.mark.db
async def test_stats_use_utc_until_the_user_sets_a_zone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    carl = await user(sessions, "carl", zone=None)
    async with sessions() as session:
        trackers = TrackersService(session)
        steps = await trackers.create_tracker(carl, "Steps", kind="custom", unit="count")
        await trackers.log(carl, steps.id, 1000, at=datetime(2026, 10, 15, 23, 30, tzinfo=UTC))
        stats = await trackers.stats(
            carl,
            steps.id,
            start=THURSDAY,
            end=THURSDAY,
            now=datetime(2026, 10, 16, tzinfo=UTC),
        )
        assert stats.time_zone == "UTC"
        assert stats.buckets[0].sum == Decimal(1000)
