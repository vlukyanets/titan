"""Reminders: when they fire next, and the occurrence they are about."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base, str_enum
from titan.storage.ids import uuid7


class ReminderStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    FIRED = "fired"
    SNOOZED = "snoozed"
    DISMISSED = "dismissed"


class LinkType(enum.StrEnum):
    TASK = "task"
    EVENT = "event"
    TRACKER = "tracker"


# Reminders that can still fire.
PENDING = (ReminderStatus.SCHEDULED, ReminderStatus.SNOOZED)


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        Index("ix_reminders_owner_id_id", "owner_id", "id"),
        # The scheduler's scan for due reminders.
        Index("ix_reminders_status_fire_at", "status", "fire_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(String(500))
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occurs_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recurrence: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[ReminderStatus] = mapped_column(
        str_enum(ReminderStatus, "reminder_status"), default=ReminderStatus.SCHEDULED
    )
    # No foreign key: events and trackers live in domains that do not exist yet.
    link_type: Mapped[LinkType | None] = mapped_column(str_enum(LinkType, "reminder_link_type"))
    link_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Created and kept in step by TITAN for a task's due time; changing the task
    # changes it.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The notification of the last firing, which its Snooze and Done actions refer to.
    notification_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
