"""Reminders: scheduling, firing once, snoozing, dismissing and recurring series."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService
from titan.domains.calendar.errors import InvalidEventError
from titan.domains.calendar.service import CalendarService
from titan.domains.notifications.models import Notification, NotificationKind
from titan.domains.reminders.errors import (
    InvalidReminderError,
    NotFoundError,
    ReminderClosedError,
)
from titan.domains.reminders.models import LinkType, Reminder, ReminderStatus
from titan.domains.reminders.service import RemindersService, firing_id
from titan.domains.tasks.service import TasksService

pytestmark = pytest.mark.db

AT_9 = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


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


async def notifications(sessions: async_sessionmaker[AsyncSession]) -> list[Notification]:
    async with sessions() as session:
        return list((await session.scalars(select(Notification).order_by(Notification.id))).all())


async def test_a_one_off_reminder_fires_once(sessions: async_sessionmaker[AsyncSession]) -> None:
    anna = await user(sessions, "anna")
    boris = await user(sessions, "boris")
    async with sessions() as session:
        reminders = RemindersService(session)
        reminder = await reminders.create(anna, "  Call   the dentist ", AT_9)
        assert (reminder.text, reminder.status) == ("Call the dentist", ReminderStatus.SCHEDULED)
        assert reminder.occurs_at == reminder.fire_at == AT_9
        assert await reminders.due(AT_9 - timedelta(seconds=1)) == []
        assert await reminders.due(AT_9) == [reminder.id]
        assert await reminders.fire(reminder.id, now=AT_9 - timedelta(minutes=1)) is None

        fired = await reminders.fire(reminder.id, now=AT_9)
        assert fired is not None
        assert (fired.status, fired.fired_at) == (ReminderStatus.FIRED, AT_9)
        assert await reminders.fire(reminder.id, now=AT_9) is None
        assert await reminders.due(AT_9 + timedelta(hours=1)) == []
        with pytest.raises(NotFoundError):
            await reminders.get(boris, reminder.id)

    sent = await notifications(sessions)
    assert [(n.kind, n.body, n.data) for n in sent] == [
        (NotificationKind.REMINDER, "Call the dentist", {"reminder_id": str(reminder.id)})
    ]
    # Every node derives the same id for this firing, so copies collapse (ADR 0006).
    assert fired.notification_id == sent[0].id == firing_id(reminder.id, AT_9)


async def test_two_processes_racing_fire_it_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        reminder = await RemindersService(session).create(anna, "Stand up", AT_9)

    async def fire() -> bool:
        async with sessions() as session:
            return await RemindersService(session).fire(reminder.id, now=AT_9) is not None

    results = await asyncio.gather(*(fire() for _ in range(5)))
    assert sorted(results) == [False] * 4 + [True]
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(Notification))
    assert count == 1


async def test_a_recurring_reminder_moves_on_and_snoozes_a_copy(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        reminders = RemindersService(session)
        pills = await reminders.create(anna, "Pills", AT_9, recurrence="FREQ=DAILY")
        fired = await reminders.fire(pills.id, now=AT_9 + timedelta(seconds=20))
        assert fired is not None
        assert fired.status is ReminderStatus.SCHEDULED
        assert fired.occurs_at == fired.fire_at == AT_9 + timedelta(days=1)

        # The notification's Snooze leaves the series alone.
        snoozed = await reminders.snooze(anna, pills.id, 15, now=AT_9 + timedelta(minutes=1))
        assert snoozed.id != pills.id
        assert (snoozed.status, snoozed.recurrence) == (ReminderStatus.SNOOZED, None)
        assert snoozed.fire_at == AT_9 + timedelta(minutes=16)
        assert (await reminders.get(anna, pills.id)).fire_at == AT_9 + timedelta(days=1)
        # Done acknowledges the occurrence; the series goes on.
        assert (await reminders.dismiss(anna, pills.id)).status is ReminderStatus.SCHEDULED

        # Three days offline: missed occurrences are skipped, not fired in a burst.
        late = AT_9 + timedelta(days=4, hours=2)
        assert set(await reminders.due(late)) == {pills.id, snoozed.id}
        again = await reminders.fire(pills.id, now=late)
        assert again is not None
        assert again.fire_at == AT_9 + timedelta(days=5)

        # Clearing the rule turns it into a one-off at its current time.
        one_off = await reminders.update(anna, pills.id, {"recurrence": None})
        assert (one_off.recurrence, one_off.status) == (None, ReminderStatus.SCHEDULED)
        assert one_off.occurs_at == AT_9 + timedelta(days=5)


async def test_snooze_dismiss_and_validation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        reminders = RemindersService(session)
        tea = await reminders.create(anna, "Tea is ready", AT_9)
        await reminders.fire(tea.id, now=AT_9)
        snoozed = await reminders.snooze(anna, tea.id, now=AT_9)
        assert snoozed.id == tea.id
        assert (snoozed.status, snoozed.fire_at) == (
            ReminderStatus.SNOOZED,
            AT_9 + timedelta(minutes=10),
        )
        assert await reminders.due(AT_9 + timedelta(minutes=10)) == [tea.id]
        # The snoozed firing is a new notification, not a copy of the first.
        refired = await reminders.fire(tea.id, now=AT_9 + timedelta(minutes=10))
        assert refired is not None
        assert refired.notification_id == firing_id(tea.id, AT_9 + timedelta(minutes=10))
        assert refired.notification_id != firing_id(tea.id, AT_9)
        assert refired.status is ReminderStatus.FIRED
        dismissed = await reminders.dismiss(anna, tea.id)
        assert dismissed.status is ReminderStatus.DISMISSED
        assert await reminders.due(AT_9 + timedelta(days=1)) == []
        with pytest.raises(ReminderClosedError):
            await reminders.snooze(anna, tea.id)
        moved = await reminders.update(anna, tea.id, {"fire_at": AT_9 + timedelta(days=2)})
        assert moved.status is ReminderStatus.SCHEDULED

        with pytest.raises(InvalidReminderError):
            await reminders.snooze(anna, tea.id, 0)
        for bad in (
            {"text": " ", "fire_at": AT_9},
            {"text": "x", "fire_at": datetime(2026, 9, 21, 9)},
            {"text": "x", "fire_at": AT_9, "recurrence": "FREQ=SECONDLY"},
            {"text": "x", "fire_at": AT_9, "link_type": LinkType.TASK},
            {"text": "x", "fire_at": AT_9, "link_type": LinkType.TASK, "link_id": uuid.uuid4()},
        ):
            with pytest.raises(InvalidReminderError):
                await reminders.create(anna, **bad)  # type: ignore[arg-type]

        task = await TasksService(session).create_task(anna, "Renew the passport")
        linked = await reminders.create(
            anna, "Passport", AT_9, link_type=LinkType.TASK, link_id=task.id
        )
        assert (linked.link_type, linked.link_id) == (LinkType.TASK, task.id)
        await reminders.delete(anna, linked.id)
        assert [r.id for r in await reminders.reminders(anna)] == [tea.id]


async def test_a_daily_reminder_keeps_its_local_time(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    kyiv = ZoneInfo("Europe/Kyiv")
    first = datetime(2026, 10, 24, 8, 0, tzinfo=kyiv)
    async with sessions() as session:
        await CalendarService(session).set_prefs(
            anna,
            time_zone="Europe/Kyiv",
            work_start=time(9),
            work_end=time(17),
            work_days=[1, 2, 3, 4, 5],
            buffer_minutes=10,
        )
        reminders = RemindersService(session)
        walk = await reminders.create(anna, "Walk the dog", first, recurrence="FREQ=DAILY")
        fired = await reminders.fire(walk.id, now=first)
        assert fired is not None
        assert fired.fire_at.astimezone(kyiv) == datetime(2026, 10, 25, 8, 0, tzinfo=kyiv)


async def default_reminder(session: AsyncSession, task_id: uuid.UUID) -> Reminder | None:
    found: Reminder | None = await session.scalar(
        select(Reminder).where(Reminder.link_id == task_id, Reminder.is_default.is_(True))
    )
    return found


async def test_tasks_with_a_due_time_get_a_default_reminder(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    soon = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    async with sessions() as session:
        tasks = TasksService(session)
        call = await tasks.create_task(anna, "Call the bank", due_at=soon)
        reminder = await default_reminder(session, call.id)
        assert reminder is not None
        assert (reminder.owner_id, reminder.text) == (anna, "Call the bank")
        assert reminder.fire_at == soon - timedelta(minutes=15)

        await tasks.update_task(
            anna, call.id, {"due_at": soon + timedelta(hours=1), "title": "Call the bank back"}
        )
        moved = await default_reminder(session, call.id)
        assert moved is not None
        assert moved.id == reminder.id
        assert moved.fire_at == soon + timedelta(minutes=45)
        assert moved.text == "Call the bank back"

        # Too close to its due time, or without one: no reminder.
        rushed = await tasks.create_task(
            anna, "Rush", due_at=datetime.now(UTC) + timedelta(minutes=5)
        )
        assert await default_reminder(session, rushed.id) is None
        await tasks.update_task(anna, call.id, {"due_at": None})
        assert await default_reminder(session, call.id) is None

        # Completing or deleting the task removes it; the next occurrence gets its own.
        daily = await tasks.create_task(anna, "Stretch", due_at=soon, recurrence="FREQ=DAILY")
        done = await tasks.complete_task(anna, daily.id)
        assert await default_reminder(session, daily.id) is None
        assert done.next is not None
        following = await default_reminder(session, done.next.id)
        assert following is not None
        assert following.fire_at == soon + timedelta(days=1, minutes=-15)
        await tasks.delete_task(anna, done.next.id)
        assert await default_reminder(session, done.next.id) is None


async def test_the_lead_time_is_a_preference(sessions: async_sessionmaker[AsyncSession]) -> None:
    anna = await user(sessions, "anna")
    soon = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=3)
    async with sessions() as session:
        calendar = CalendarService(session)

        async def lead(minutes: int | None) -> None:
            await calendar.set_prefs(
                anna,
                time_zone="UTC",
                work_start=time(9),
                work_end=time(17),
                work_days=[1, 2, 3, 4, 5],
                buffer_minutes=10,
                default_reminder_minutes=minutes,
            )

        await lead(60)
        tasks = TasksService(session)
        early = await tasks.create_task(anna, "Pack", due_at=soon)
        reminder = await default_reminder(session, early.id)
        assert reminder is not None
        assert reminder.fire_at == soon - timedelta(hours=1)
        await lead(None)
        quiet = await tasks.create_task(anna, "Read", due_at=soon)
        assert await default_reminder(session, quiet.id) is None
        with pytest.raises(InvalidEventError):
            await lead(0)
