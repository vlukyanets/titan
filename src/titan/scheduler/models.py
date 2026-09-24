"""Leases that decide which node runs a sweep."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base


class SchedulerLease(Base):
    __tablename__ = "scheduler_leases"

    # The sweep, such as "reminders".
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Written by the preferred node on every tick, held or not, so a
    # non-preferred holder knows to hand the lease back.
    preferred_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
