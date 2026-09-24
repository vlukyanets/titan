"""One record per agent session that reported usage. Numbers only, no content."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base
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
