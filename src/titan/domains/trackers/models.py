"""Trackers and their entries."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base, str_enum
from titan.storage.ids import uuid7

# Up to 10^14 with four decimal places: exact money, with room to spare.
VALUE = Numeric(18, 4)


class TrackerKind(enum.StrEnum):
    HABIT = "habit"
    HEALTH = "health"
    FINANCE = "finance"
    CUSTOM = "custom"


class Period(enum.StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class Direction(enum.StrEnum):
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class Tracker(Base):
    __tablename__ = "trackers"
    __table_args__ = (
        Index("ix_trackers_owner_id", "owner_id"),
        CheckConstraint("min_value <= max_value", name="bounds"),
        CheckConstraint(
            "(target_value IS NULL) = (target_period IS NULL)"
            " AND (target_value IS NULL) = (target_direction IS NULL)",
            name="target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[TrackerKind] = mapped_column(str_enum(TrackerKind, "tracker_kind"))
    unit: Mapped[str] = mapped_column(String(16))
    min_value: Mapped[Decimal | None] = mapped_column(VALUE)
    max_value: Mapped[Decimal | None] = mapped_column(VALUE)
    target_value: Mapped[Decimal | None] = mapped_column(VALUE)
    target_period: Mapped[Period | None] = mapped_column(str_enum(Period, "tracker_period"))
    target_direction: Mapped[Direction | None] = mapped_column(
        str_enum(Direction, "tracker_direction")
    )
    # RFC 5545 RRULE without DTSTART: the days a habit is due.
    schedule: Mapped[str | None] = mapped_column(String(200))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Entry(Base):
    __tablename__ = "tracker_entries"
    # Every read is one tracker's entries over a time range.
    __table_args__ = (Index("ix_tracker_entries_tracker_id_at", "tracker_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    tracker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trackers.id"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[Decimal] = mapped_column(VALUE)
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    category: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
