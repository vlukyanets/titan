"""Events, who attends them, and each user's planning preferences."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, time

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base, str_enum
from titan.storage.ids import uuid7


class EventKind(enum.StrEnum):
    EVENT = "event"
    TIME_BLOCK = "time_block"


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        # Window queries: an owner's events that start before the window ends.
        Index("ix_events_owner_id_starts_at", "owner_id", "starts_at"),
        CheckConstraint("ends_at > starts_at", name="ends_after_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[EventKind] = mapped_column(
        str_enum(EventKind, "event_kind"), default=EventKind.EVENT
    )
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    location: Mapped[str | None] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # IANA zone that fixes the local time of repeats and the days of all-day events.
    time_zone: Mapped[str] = mapped_column(String(64))
    recurrence: Mapped[str | None] = mapped_column(String(200))
    # A time block's task; no foreign key, so deleting the task keeps the block.
    task_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EventAttendee(Base):
    """A user who sees an event in their calendar, besides its owner."""

    __tablename__ = "event_attendees"
    __table_args__ = (Index("ix_event_attendees_user_id", "user_id"),)

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)


class PlanningPrefs(Base):
    __tablename__ = "planning_prefs"
    __table_args__ = (
        CheckConstraint("work_end > work_start", name="work_hours"),
        CheckConstraint("buffer_minutes BETWEEN 0 AND 240", name="buffer_minutes"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    time_zone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    work_start: Mapped[time] = mapped_column(Time, default=time(9), server_default="09:00")
    work_end: Mapped[time] = mapped_column(Time, default=time(17), server_default="17:00")
    # ISO weekdays, 1 = Monday.
    work_days: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), default=lambda: [1, 2, 3, 4, 5], server_default="{1,2,3,4,5}"
    )
    buffer_minutes: Mapped[int] = mapped_column(SmallInteger, default=10, server_default="10")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
