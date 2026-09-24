"""Monthly budgets: the state of a user's month and the notifications it sends.

Chat turns and scheduled workflows read the state when they start; the agent
layer calls `check()` after it records a session, which notifies on the way up.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.accounts.models import User
from titan.domains.accounts.service import Principal
from titan.domains.notifications.models import Notification, NotificationKind
from titan.domains.notifications.service import NotificationsService
from titan.domains.usage.errors import ForbiddenError, InvalidBudgetError, NotFoundError
from titan.domains.usage.models import Budget, UsageRecord
from titan.domains.usage.service import current_month, month_range

MAX_LIMIT = Decimal("100000")
WARNING_SHARE = 0.8
CENT = Decimal("0.01")
NOTIFICATION_NAMESPACE = uuid.UUID("65f3b2e4-3762-4d66-b53c-1f4c57b8b3f6")


class BudgetState(enum.StrEnum):
    OK = "ok"
    WARNING = "warning"
    EXCEEDED = "exceeded"


@dataclass(frozen=True)
class BudgetStatus:
    month: str
    limit_usd: Decimal | None
    spent_usd: float

    @property
    def state(self) -> BudgetState:
        if self.limit_usd is None:
            return BudgetState.OK
        limit = float(self.limit_usd)
        if self.spent_usd >= limit:
            return BudgetState.EXCEEDED
        if self.spent_usd >= limit * WARNING_SHARE:
            return BudgetState.WARNING
        return BudgetState.OK

    @property
    def exceeded(self) -> bool:
        return self.state is BudgetState.EXCEEDED


@dataclass(frozen=True)
class MemberBudget:
    user_id: uuid.UUID
    username: str
    status: BudgetStatus


def normalize_limit(limit: Decimal | None) -> Decimal | None:
    if limit is None:
        return None
    if not limit.is_finite() or not 0 <= limit <= MAX_LIMIT:
        raise InvalidBudgetError(f"the limit must be between 0 and {MAX_LIMIT} USD")
    return limit.quantize(CENT, rounding=ROUND_HALF_UP)


def notification_id(user_id: uuid.UUID, status: BudgetStatus) -> uuid.UUID:
    """The same on every node, so one state of one month and limit notifies once."""
    return uuid.uuid5(
        NOTIFICATION_NAMESPACE,
        f"{user_id}:{status.month}:{status.state.value}:{status.limit_usd}",
    )


def _message(status: BudgetStatus) -> tuple[str, str]:
    used = f"{status.spent_usd:.2f} of {status.limit_usd} USD used in {status.month}."
    if status.exceeded:
        return (
            "Monthly budget reached",
            f"{used} Chat uses the fast model and scheduled workflows are paused until "
            "the owner raises the limit or the month ends.",
        )
    return (
        "80 % of the monthly budget used",
        f"{used} At 100 % chat switches to the fast model and scheduled workflows pause.",
    )


class BudgetService:
    def __init__(
        self, session: AsyncSession, *, notifications: NotificationsService | None = None
    ) -> None:
        self.session = session
        self.notifications = notifications

    async def status(self, user_id: uuid.UUID, *, now: datetime | None = None) -> BudgetStatus:
        month = current_month(now)
        start, end = month_range(month)
        spent = await self.session.scalar(
            select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)).where(
                UsageRecord.user_id == user_id,
                UsageRecord.created_at >= start,
                UsageRecord.created_at < end,
            )
        )
        limit = await self.session.scalar(select(Budget.limit_usd).where(Budget.user_id == user_id))
        return BudgetStatus(month, limit, float(spent or 0.0))

    async def household(
        self, actor: Principal | None, *, now: datetime | None = None
    ) -> list[MemberBudget]:
        """Every user's budget this month. Owner only; `actor` None is a node's CLI."""
        _require_owner(actor)
        month = current_month(now)
        start, end = month_range(month)
        spent = (
            select(UsageRecord.user_id, func.sum(UsageRecord.cost_usd).label("spent"))
            .where(UsageRecord.created_at >= start, UsageRecord.created_at < end)
            .group_by(UsageRecord.user_id)
            .subquery()
        )
        rows = await self.session.execute(
            select(User.id, User.username, Budget.limit_usd, spent.c.spent)
            .outerjoin(Budget, Budget.user_id == User.id)
            .outerjoin(spent, spent.c.user_id == User.id)
            .order_by(User.username)
        )
        return [
            MemberBudget(user_id, username, BudgetStatus(month, limit, float(total or 0.0)))
            for user_id, username, limit, total in rows.tuples()
        ]

    async def set_limit(
        self,
        actor: Principal | None,
        user_id: uuid.UUID,
        limit: Decimal | None,
        *,
        now: datetime | None = None,
    ) -> MemberBudget:
        """Set a user's monthly limit, or remove it with None. Owner only."""
        _require_owner(actor)
        limit = normalize_limit(limit)
        user = await self.session.get(User, user_id)
        if user is None:
            raise NotFoundError("user not found")
        if limit is None:
            await self.session.execute(delete(Budget).where(Budget.user_id == user_id))
        else:
            stmt = insert(Budget).values(user_id=user_id, limit_usd=limit)
            await self.session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Budget.user_id],
                    set_={"limit_usd": stmt.excluded.limit_usd, "updated_at": func.now()},
                )
            )
        await self.session.commit()
        # A lower limit can put the user over it at once; they hear about it now.
        return MemberBudget(user.id, user.username, await self.check(user_id, now=now))

    async def check(self, user_id: uuid.UUID, *, now: datetime | None = None) -> BudgetStatus:
        """The user's state, notifying them the first time it is warning or exceeded."""
        status = await self.status(user_id, now=now)
        if status.state is BudgetState.OK or self.notifications is None:
            return status
        nid = notification_id(user_id, status)
        if await self.session.get(Notification, nid) is not None:
            return status
        title, body = _message(status)
        try:
            await self.notifications.notify(
                user_id,
                NotificationKind.BUDGET,
                title,
                body,
                {"month": status.month, "state": status.state.value},
                notification_id=nid,
            )
        except IntegrityError:
            # Another session of the same user sent it first.
            await self.session.rollback()
        return status


def _require_owner(actor: Principal | None) -> None:
    if actor is not None and not actor.is_owner:
        raise ForbiddenError("only the owner manages budgets")
