"""Default reminders of tasks and events (docs/spec/domains/reminders.md#delivery).

Tasks and events call these whenever they change. They import no service, so
the tasks and calendar services can use them while the reminders service
imports tasks.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.calendar.models import Event, EventKind, PlanningPrefs
from titan.domains.reminders.models import LinkType, Reminder, ReminderStatus
from titan.domains.tasks import recurrence
from titan.domains.tasks.models import Task, TaskStatus

DEFAULT_LEAD_MINUTES = 15
_OPEN = (TaskStatus.TODO, TaskStatus.DOING)


async def lead_minutes(session: AsyncSession, user_id: uuid.UUID) -> int | None:
    prefs = await session.get(PlanningPrefs, user_id)
    return DEFAULT_LEAD_MINUTES if prefs is None else prefs.default_reminder_minutes


async def sync_task_reminder(
    session: AsyncSession, task: Task, *, now: datetime | None = None
) -> Reminder | None:
    """Create, move or remove the task's default reminder; flushes, never commits."""
    now = now or datetime.now(UTC)
    existing = await session.scalar(
        select(Reminder)
        .where(
            Reminder.link_type == LinkType.TASK,
            Reminder.link_id == task.id,
            Reminder.is_default.is_(True),
        )
        .with_for_update()
    )
    minutes = await lead_minutes(session, task.owner_id)
    if task.status not in _OPEN or task.due_at is None or minutes is None:
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        return None
    fire_at = task.due_at - timedelta(minutes=minutes)
    if existing is None:
        # Too late to remind ahead of the due time.
        if fire_at <= now:
            return None
        existing = Reminder(
            owner_id=task.owner_id,
            text=task.title,
            fire_at=fire_at,
            occurs_at=fire_at,
            link_type=LinkType.TASK,
            link_id=task.id,
            is_default=True,
        )
        session.add(existing)
    else:
        existing.text = task.title
        if existing.occurs_at != fire_at:
            existing.fire_at = existing.occurs_at = fire_at
            existing.status = ReminderStatus.SCHEDULED if fire_at > now else ReminderStatus.FIRED
            existing.updated_at = now
    await session.flush()
    return existing


async def drop_task_reminders(session: AsyncSession, task_id: uuid.UUID) -> None:
    """Remove the default reminder of a task being deleted; flushes."""
    await session.execute(
        delete(Reminder).where(
            Reminder.link_type == LinkType.TASK,
            Reminder.link_id == task_id,
            Reminder.is_default.is_(True),
        )
    )


def _event_fire_at(event: Event, minutes: int, now: datetime) -> datetime | None:
    """When the next occurrence that is still ahead by `minutes` should remind."""
    lead = timedelta(minutes=minutes)
    start: datetime | None = event.starts_at
    if event.starts_at - lead <= now:
        if not event.recurrence:
            return None
        start = recurrence.next_occurrence(
            event.recurrence, event.starts_at, now + lead, ZoneInfo(event.time_zone)
        )
    return None if start is None else start - lead


async def sync_event_reminders(
    session: AsyncSession,
    event: Event,
    members: Iterable[uuid.UUID],
    *,
    now: datetime | None = None,
) -> None:
    """Keep one default reminder per member (the owner and the attendees); flushes.

    A recurring event's reminder repeats with the event's rule, anchored on the
    reminder's own time. All-day events and time blocks get none.
    """
    # ponytail: the reminder repeats in its owner's zone, not the event's; an
    # attendee in another zone can be an hour off for the weeks the two zones'
    # daylight saving dates differ. Anchor on the event's zone if that matters.
    now = now or datetime.now(UTC)
    existing = {
        r.owner_id: r
        for r in await session.scalars(
            select(Reminder)
            .where(
                Reminder.link_type == LinkType.EVENT,
                Reminder.link_id == event.id,
                Reminder.is_default.is_(True),
            )
            .with_for_update()
        )
    }
    wanted = [event.owner_id, *members]
    for user_id in wanted:
        minutes = await lead_minutes(session, user_id)
        fire_at = None
        if minutes is not None and event.kind is EventKind.EVENT and not event.all_day:
            fire_at = _event_fire_at(event, minutes, now)
        reminder = existing.pop(user_id, None)
        if fire_at is None:
            if reminder is not None:
                await session.delete(reminder)
            continue
        if reminder is None:
            session.add(
                Reminder(
                    owner_id=user_id,
                    text=event.title,
                    fire_at=fire_at,
                    occurs_at=fire_at,
                    recurrence=event.recurrence,
                    link_type=LinkType.EVENT,
                    link_id=event.id,
                    is_default=True,
                )
            )
            continue
        reminder.text = event.title
        if reminder.occurs_at != fire_at or reminder.recurrence != event.recurrence:
            reminder.fire_at = reminder.occurs_at = fire_at
            reminder.recurrence = event.recurrence
            reminder.status = ReminderStatus.SCHEDULED
            reminder.updated_at = now
    for gone in existing.values():
        await session.delete(gone)
    await session.flush()


async def drop_event_reminders(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Remove the default reminders of an event being deleted; flushes."""
    await session.execute(
        delete(Reminder).where(
            Reminder.link_type == LinkType.EVENT,
            Reminder.link_id == event_id,
            Reminder.is_default.is_(True),
        )
    )
