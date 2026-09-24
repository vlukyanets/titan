"""Recording agent sessions' usage and adding it up per month."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.accounts.models import User
from titan.domains.accounts.service import Principal
from titan.domains.usage.errors import ForbiddenError, InvalidMonthError
from titan.domains.usage.models import UsageRecord

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def current_month(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")


def month_range(month: str) -> tuple[datetime, datetime]:
    """[start, end) of a calendar month in UTC, from "YYYY-MM"."""
    match = _MONTH.match(month)
    if match is None:
        raise InvalidMonthError(f"month must look like 2026-09, not {month!r}")
    year, number = int(match[1]), int(match[2])
    start = datetime(year, number, 1, tzinfo=UTC)
    end = datetime(year + number // 12, number % 12 + 1, 1, tzinfo=UTC)
    return start, end


@dataclass
class Totals:
    sessions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: Totals) -> None:
        self.sessions += other.sessions
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cost_usd += other.cost_usd


@dataclass
class MonthUsage:
    month: str
    total: Totals = field(default_factory=Totals)
    by_model: dict[str, Totals] = field(default_factory=dict)


@dataclass
class MemberUsage:
    user_id: uuid.UUID
    username: str
    usage: MonthUsage


_SUMS = (
    func.count(UsageRecord.id),
    func.coalesce(func.sum(UsageRecord.input_tokens), 0),
    func.coalesce(func.sum(UsageRecord.output_tokens), 0),
    func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0),
    func.coalesce(func.sum(UsageRecord.cache_creation_tokens), 0),
    func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
)


def _totals(row: tuple[int, int, int, int, int, float]) -> Totals:
    sessions, input_tokens, output_tokens, cache_read, cache_creation, cost = row
    return Totals(
        sessions=int(sessions),
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cache_read_tokens=int(cache_read),
        cache_creation_tokens=int(cache_creation),
        cost_usd=float(cost),
    )


class UsageService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        user_id: uuid.UUID,
        source: str,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cost_usd: float | None = None,
        reference: uuid.UUID | None = None,
    ) -> UsageRecord:
        record = UsageRecord(
            user_id=user_id,
            source=source[:32],
            reference=reference,
            model=model[:64],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cost_usd=cost_usd,
        )
        self.session.add(record)
        await self.session.commit()
        return record

    async def month(self, actor: Principal, month: str) -> MonthUsage:
        """The caller's totals for a month, overall and per model."""
        start, end = month_range(month)
        rows = await self.session.execute(
            select(UsageRecord.model, *_SUMS)
            .where(
                UsageRecord.user_id == actor.user_id,
                UsageRecord.created_at >= start,
                UsageRecord.created_at < end,
            )
            .group_by(UsageRecord.model)
            .order_by(UsageRecord.model)
        )
        usage = MonthUsage(month)
        for model, *sums in rows.tuples():
            totals = _totals(tuple(sums))  # type: ignore[arg-type]
            usage.by_model[model] = totals
            usage.total.add(totals)
        return usage

    async def household(self, actor: Principal | None, month: str) -> list[MemberUsage]:
        """Every user's totals for a month. Owner only; `actor` None is a node's CLI."""
        if actor is not None and not actor.is_owner:
            raise ForbiddenError("only the owner sees everyone's usage")
        start, end = month_range(month)
        rows = await self.session.execute(
            select(User.id, User.username, UsageRecord.model, *_SUMS)
            .join(
                UsageRecord,
                (UsageRecord.user_id == User.id)
                & (UsageRecord.created_at >= start)
                & (UsageRecord.created_at < end),
                isouter=True,
            )
            .group_by(User.id, User.username, UsageRecord.model)
            .order_by(User.username, UsageRecord.model)
        )
        members: dict[uuid.UUID, MemberUsage] = {}
        for user_id, username, model, *sums in rows.tuples():
            member = members.setdefault(user_id, MemberUsage(user_id, username, MonthUsage(month)))
            if model is None:
                continue  # a user without records this month
            totals = _totals(tuple(sums))  # type: ignore[arg-type]
            member.usage.by_model[model] = totals
            member.usage.total.add(totals)
        return list(members.values())
