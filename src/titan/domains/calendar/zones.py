"""A user's time zone, for anything that repeats at a local time.

Kept apart from the calendar service so tasks and reminders can use it without
importing the calendar (which imports tasks).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.calendar.models import PlanningPrefs

UTC_ZONE = ZoneInfo("UTC")


async def user_zone(session: AsyncSession, user_id: uuid.UUID) -> ZoneInfo:
    """The zone from the user's planning preferences; UTC until they set one."""
    name = await session.scalar(
        select(PlanningPrefs.time_zone).where(PlanningPrefs.user_id == user_id)
    )
    try:
        return ZoneInfo(name) if name else UTC_ZONE
    except (ZoneInfoNotFoundError, ValueError):
        return UTC_ZONE


def parse_local(text: str, zone: ZoneInfo) -> datetime:
    """ISO 8601; a time without an offset is in `zone`. Raises ValueError."""
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)


def show_local(value: datetime | None, zone: ZoneInfo) -> str:
    return "" if value is None else value.astimezone(zone).strftime("%Y-%m-%d %H:%M")
