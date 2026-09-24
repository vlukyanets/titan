"""The default reminder of a task with a due time (docs/spec/domains/reminders.md).

Tasks call this whenever they change. It imports no service, so the tasks
service can use it while the reminders service imports tasks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.calendar.models import PlanningPrefs
from titan.domains.reminders.models import LinkType, Reminder, ReminderStatus
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
