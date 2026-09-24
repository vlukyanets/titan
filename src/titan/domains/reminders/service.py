"""Reminders: scheduling, snoozing, dismissing and firing.

Firing changes the reminder in the same transaction that stores its
notification, so an occurrence that has fired cannot fire again on this node.
Which node fires a due reminder is the scheduler's job (ADR 0006).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.notifications.models import NotificationKind
from titan.domains.notifications.service import NotificationsService
from titan.domains.reminders.errors import (
    InvalidReminderError,
    NotFoundError,
    ReminderClosedError,
)
from titan.domains.reminders.models import PENDING, LinkType, Reminder, ReminderStatus
from titan.domains.tasks import errors as tasks_errors
from titan.domains.tasks import recurrence
from titan.domains.tasks.service import TasksService

DEFAULT_PAGE = 50
MAX_PAGE = 100
TEXT_LENGTH = 500
DEFAULT_SNOOZE_MINUTES = 10
MAX_SNOOZE_MINUTES = 7 * 24 * 60
REMINDER_FIELDS = frozenset({"text", "fire_at", "recurrence"})
# Namespace of the deterministic notification ids of reminder firings.
FIRING_NAMESPACE = uuid.UUID("6c1f4a8e-2b0d-4f4b-9a57-0d7a1d3c9e21")


def _now() -> datetime:
    return datetime.now(UTC)


def firing_id(reminder_id: uuid.UUID, fire_at: datetime) -> uuid.UUID:
    """The notification id of one firing: the same on every node (ADR 0006)."""
    return uuid.uuid5(FIRING_NAMESPACE, f"{reminder_id}:{fire_at.astimezone(UTC).isoformat()}")


def _text(value: object) -> str:
    text = "" if value is None else " ".join(str(value).split())
    if not text:
        raise InvalidReminderError("the text is empty")
    if len(text) > TEXT_LENGTH:
        raise InvalidReminderError(f"the text is longer than {TEXT_LENGTH} characters")
    return text


def _when(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidReminderError("fire_at is a time with a time zone")
    return value.astimezone(UTC)


def _rule(value: object) -> str | None:
    if value is None:
        return None
    try:
        return recurrence.normalize(str(value))
    except tasks_errors.InvalidRecurrenceError as exc:
        raise InvalidReminderError(str(exc)) from None


class RemindersService:
    def __init__(
        self, session: AsyncSession, *, notifications: NotificationsService | None = None
    ) -> None:
        self.session = session
        # Needed only to fire; built on the same session so the notification and
        # the reminder's new state commit together.
        self.notifications = notifications or NotificationsService(session)

    async def create(
        self,
        actor: uuid.UUID,
        text: str,
        fire_at: datetime,
        *,
        recurrence: str | None = None,
        link_type: LinkType | None = None,
        link_id: uuid.UUID | None = None,
    ) -> Reminder:
        when = _when(fire_at)
        if (link_type is None) != (link_id is None):
            raise InvalidReminderError("a link needs both link_type and link_id")
        if link_type is LinkType.TASK and link_id is not None:
            try:
                await TasksService(self.session).get_task(actor, link_id)
            except tasks_errors.NotFoundError:
                raise InvalidReminderError("the linked task does not exist") from None
        reminder = Reminder(
            owner_id=actor,
            text=_text(text),
            fire_at=when,
            occurs_at=when,
            recurrence=_rule(recurrence),
            link_type=link_type,
            link_id=link_id,
        )
        self.session.add(reminder)
        await self.session.commit()
        return reminder

    async def reminders(
        self,
        actor: uuid.UUID,
        *,
        status: ReminderStatus | None = None,
        before: uuid.UUID | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[Reminder]:
        query = select(Reminder).where(Reminder.owner_id == actor)
        if status is not None:
            query = query.where(Reminder.status == status)
        if before is not None:
            query = query.where(Reminder.id < before)
        query = query.order_by(Reminder.id.desc()).limit(max(1, min(limit, MAX_PAGE)))
        return list((await self.session.scalars(query)).all())

    async def get(
        self, actor: uuid.UUID, reminder_id: uuid.UUID, *, lock: bool = False
    ) -> Reminder:
        query = select(Reminder).where(Reminder.id == reminder_id, Reminder.owner_id == actor)
        if lock:
            query = query.with_for_update()
        reminder = await self.session.scalar(query)
        if reminder is None:
            raise NotFoundError("reminder not found")
        return reminder

    async def update(
        self, actor: uuid.UUID, reminder_id: uuid.UUID, changes: Mapping[str, Any]
    ) -> Reminder:
        unknown = set(changes) - REMINDER_FIELDS
        if unknown:
            raise InvalidReminderError(f"a reminder has no field {sorted(unknown)[0]}")
        reminder = await self.get(actor, reminder_id, lock=True)
        text = _text(changes["text"]) if "text" in changes else reminder.text
        when = _when(changes["fire_at"]) if "fire_at" in changes else None
        rule = _rule(changes["recurrence"]) if "recurrence" in changes else reminder.recurrence
        reminder.text = text
        if when is not None or rule != reminder.recurrence:
            # Rescheduled: a new series (or a new one-off) starts at fire_at.
            start = when or reminder.fire_at
            reminder.recurrence = rule
            reminder.fire_at = reminder.occurs_at = start
            reminder.status = ReminderStatus.SCHEDULED
        reminder.updated_at = _now()
        await self.session.commit()
        return reminder

    async def snooze(
        self,
        actor: uuid.UUID,
        reminder_id: uuid.UUID,
        minutes: int = DEFAULT_SNOOZE_MINUTES,
        *,
        now: datetime | None = None,
    ) -> Reminder:
        """Make the reminder fire again later; answers the reminder that will."""
        if not 0 < minutes <= MAX_SNOOZE_MINUTES:
            raise InvalidReminderError(f"snooze for 1 to {MAX_SNOOZE_MINUTES} minutes")
        reminder = await self.get(actor, reminder_id, lock=True)
        if reminder.status is ReminderStatus.DISMISSED:
            raise ReminderClosedError("the reminder was dismissed")
        now = now or _now()
        later = now + timedelta(minutes=minutes)
        if reminder.recurrence:
            # The series has moved on; the snoozed occurrence continues on its own.
            snoozed = Reminder(
                owner_id=reminder.owner_id,
                text=reminder.text,
                fire_at=later,
                occurs_at=reminder.fired_at or now,
                status=ReminderStatus.SNOOZED,
                link_type=reminder.link_type,
                link_id=reminder.link_id,
            )
            self.session.add(snoozed)
        else:
            snoozed = reminder
            snoozed.fire_at = later
            snoozed.status = ReminderStatus.SNOOZED
            snoozed.updated_at = now
        await self.session.commit()
        return snoozed

    async def dismiss(self, actor: uuid.UUID, reminder_id: uuid.UUID) -> Reminder:
        reminder = await self.get(actor, reminder_id, lock=True)
        # A recurring reminder's occurrence is over once it fired; the series goes on.
        if reminder.recurrence is None:
            reminder.status = ReminderStatus.DISMISSED
            reminder.updated_at = _now()
            await self.session.commit()
        return reminder

    async def delete(self, actor: uuid.UUID, reminder_id: uuid.UUID) -> None:
        reminder = await self.get(actor, reminder_id, lock=True)
        await self.session.delete(reminder)
        await self.session.commit()

    # ------------------------------------------------------------- firing

    async def due(self, now: datetime | None = None, *, limit: int = 100) -> list[uuid.UUID]:
        """Ids of reminders whose time has come, oldest first."""
        rows = await self.session.scalars(
            select(Reminder.id)
            .where(Reminder.status.in_(PENDING), Reminder.fire_at <= (now or _now()))
            .order_by(Reminder.fire_at)
            .limit(limit)
        )
        return list(rows.all())

    async def fire(self, reminder_id: uuid.UUID, *, now: datetime | None = None) -> Reminder | None:
        """Fire one due reminder. `None` if it is not due (any more)."""
        now = now or _now()
        # SKIP LOCKED: a second process firing the same reminder moves on instead
        # of waiting and then finding it already fired.
        reminder = await self.session.scalar(
            select(Reminder)
            .where(
                Reminder.id == reminder_id,
                Reminder.status.in_(PENDING),
                Reminder.fire_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        if reminder is None:
            return None
        firing = firing_id(reminder.id, reminder.fire_at)
        following = None
        if reminder.recurrence:
            following = recurrence.next_occurrence(reminder.recurrence, reminder.occurs_at, now)
        if following is not None:
            reminder.fire_at = reminder.occurs_at = following
            reminder.status = ReminderStatus.SCHEDULED
        else:
            reminder.status = ReminderStatus.FIRED
        reminder.fired_at = now
        reminder.updated_at = now
        # notify() commits, together with the reminder's new state, then pushes.
        notification = await self.notifications.notify(
            reminder.owner_id,
            NotificationKind.REMINDER,
            "Reminder",
            reminder.text,
            {"reminder_id": str(reminder.id)},
            notification_id=firing,
        )
        reminder.notification_id = notification.id
        await self.session.commit()
        return reminder
