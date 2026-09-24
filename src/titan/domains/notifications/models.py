"""Stored notifications and the devices' UnifiedPush subscriptions."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, SmallInteger, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base, str_enum
from titan.storage.ids import uuid7


class NotificationKind(enum.StrEnum):
    REMINDER = "reminder"
    APPROVAL = "approval"
    PLAN = "plan"
    BUDGET = "budget"
    SYSTEM = "system"


class Notification(Base):
    __tablename__ = "notifications"
    # Listing is per user, newest first; UUIDv7 ids sort by creation time.
    __table_args__ = (
        Index("ix_notifications_user_id_id", "user_id", "id"),
        # The worker's scan for pushes to retry.
        Index("ix_notifications_next_push_at", "next_push_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[NotificationKind] = mapped_column(str_enum(NotificationKind, "notification_kind"))
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Push retries by the worker: how many were made, and when the next is due.
    push_retries: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    next_push_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    # Contains the push server's secret topic: never log it or return it.
    endpoint: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
