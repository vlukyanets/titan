"""Usage records, one per agent session (numbers only, no content), and budgets."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base, str_enum
from titan.storage.ids import uuid7


class UsageRecord(Base):
    __tablename__ = "usage_records"
    # Totals are per user and month.
    __table_args__ = (Index("ix_usage_records_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # What ran the session: "chat", later a workflow name such as "daily_plan".
    source: Mapped[str] = mapped_column(String(32))
    # What the session produced, such as the assistant message of a chat turn.
    reference: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OwnerAlerts(enum.StrEnum):
    """Which of a user's budget states the owners are notified of."""

    OFF = "off"
    EXCEEDED = "exceeded"
    ALL = "all"


class Budget(Base):
    """A user's monthly limit. Users without a row have no cap."""

    __tablename__ = "budgets"
    __table_args__ = (CheckConstraint("limit_usd >= 0", name="limit"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    limit_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    owner_alerts: Mapped[OwnerAlerts] = mapped_column(
        str_enum(OwnerAlerts, "owner_alerts"), server_default=OwnerAlerts.EXCEEDED.value
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
