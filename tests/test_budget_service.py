"""Monthly budgets: states, owner-set limits and notifications once per state."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.test_usage_service import principal
from titan.domains.accounts.models import Role
from titan.domains.notifications.models import Notification, NotificationKind
from titan.domains.notifications.service import NotificationsService
from titan.domains.usage.budget import (
    BudgetService,
    BudgetState,
    BudgetStatus,
    normalize_limit,
)
from titan.domains.usage.errors import ForbiddenError, InvalidBudgetError, NotFoundError
from titan.domains.usage.models import OwnerAlerts, UsageRecord
from titan.domains.usage.service import UsageService


def test_states_follow_the_share_of_the_limit() -> None:
    def state(limit: str | None, spent: float) -> BudgetState:
        return BudgetStatus("2026-09", Decimal(limit) if limit else None, spent).state

    assert state(None, 1e6) is BudgetState.OK
    assert state("10.00", 7.99) is BudgetState.OK
    assert state("10.00", 8.0) is BudgetState.WARNING
    assert state("10.00", 9.99) is BudgetState.WARNING
    assert state("10.00", 10.0) is BudgetState.EXCEEDED
    assert state("0.00", 0.0) is BudgetState.EXCEEDED


def test_limits_are_cents_within_bounds() -> None:
    assert normalize_limit(Decimal("12.345")) == Decimal("12.35")
    assert normalize_limit(Decimal("0")) == Decimal("0.00")
    assert normalize_limit(None) is None
    for bad in ("-0.01", "100000.01", "NaN", "Infinity"):
        with pytest.raises(InvalidBudgetError):
            normalize_limit(Decimal(bad))


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def budget_notifications(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """The states a user was notified of about their own budget."""
    rows = await session.scalars(
        select(Notification.data["state"].astext)
        .where(
            Notification.kind == NotificationKind.BUDGET,
            Notification.user_id == user_id,
            ~Notification.data.has_key("user_id"),
        )
        .order_by(Notification.created_at, Notification.id)
    )
    return list(rows)


@pytest.mark.db
async def test_the_owner_sets_limits_and_members_cannot(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna", Role.OWNER)
    boris = await principal(sessions, "boris")
    async with sessions() as session:
        budgets = BudgetService(session)
        await UsageService(session).record(boris.user_id, "chat", "strong", cost_usd=1.5)
        set_ = await budgets.set_limit(anna, boris.user_id, Decimal("10"))
        assert (set_.username, set_.status.limit_usd, set_.status.spent_usd) == (
            "boris",
            Decimal("10.00"),
            1.5,
        )
        await budgets.set_limit(None, boris.user_id, Decimal("20"))
        assert (await budgets.status(boris.user_id)).limit_usd == Decimal("20.00")
        assert [(m.username, m.status.limit_usd) for m in await budgets.household(anna)] == [
            ("anna", None),
            ("boris", Decimal("20.00")),
        ]
        with pytest.raises(ForbiddenError):
            await budgets.set_limit(boris, boris.user_id, Decimal("99"))
        with pytest.raises(ForbiddenError):
            await budgets.household(boris)
        with pytest.raises(NotFoundError):
            await budgets.set_limit(anna, uuid.uuid4(), Decimal("1"))
        removed = await budgets.set_limit(anna, boris.user_id, None)
        assert removed.status.limit_usd is None
        assert removed.status.state is BudgetState.OK


@pytest.mark.db
async def test_each_state_notifies_once_per_month_and_limit(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna", Role.OWNER)
    boris = await principal(sessions, "boris")
    async with sessions() as session:
        usage = UsageService(session)
        budgets = BudgetService(session, notifications=NotificationsService(session))
        await budgets.set_limit(anna, boris.user_id, Decimal("10"))
        last_month = await usage.record(boris.user_id, "chat", "strong", cost_usd=50.0)
        await session.execute(
            update(UsageRecord)
            .where(UsageRecord.id == last_month.id)
            .values(created_at=datetime(2020, 1, 31, tzinfo=UTC))
        )
        await session.commit()
        assert (await budgets.check(boris.user_id)).state is BudgetState.OK

        await usage.record(boris.user_id, "chat", "strong", cost_usd=8.5)
        assert (await budgets.check(boris.user_id)).state is BudgetState.WARNING
        await usage.record(boris.user_id, "chat", "strong", cost_usd=0.5)
        await budgets.check(boris.user_id)
        assert await budget_notifications(session, boris.user_id) == ["warning"]

        await usage.record(boris.user_id, "chat", "strong", cost_usd=1.0)
        assert (await budgets.check(boris.user_id)).exceeded
        await budgets.check(boris.user_id)
        assert await budget_notifications(session, boris.user_id) == ["warning", "exceeded"]

        # A higher limit lifts the cap; reaching it again notifies again.
        raised = await budgets.set_limit(anna, boris.user_id, Decimal("12.5"))
        assert raised.status.state is BudgetState.WARNING
        assert await budget_notifications(session, boris.user_id) == [
            "warning",
            "exceeded",
            "warning",
        ]
        await usage.record(boris.user_id, "chat", "strong", cost_usd=3.0)
        await budgets.check(boris.user_id)
        assert await budget_notifications(session, boris.user_id) == [
            "warning",
            "exceeded",
            "warning",
            "exceeded",
        ]
        # With the default owner alerts, the owner hears of each exceeded limit only.
        assert await owner_heard(session, anna.user_id) == [
            "exceeded: boris reached their monthly budget",
            "exceeded: boris reached their monthly budget",
        ]


async def owner_heard(session: AsyncSession, owner_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        select(Notification.title, Notification.data["state"].astext)
        .where(Notification.user_id == owner_id, Notification.data.has_key("user_id"))
        .order_by(Notification.created_at, Notification.id)
    )
    return [f"{state}: {title}" for title, state in rows.tuples()]


@pytest.mark.db
async def test_owner_alerts_are_set_per_budget(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna", Role.OWNER)
    boris = await principal(sessions, "boris")
    async with sessions() as session:
        usage = UsageService(session)
        budgets = BudgetService(session, notifications=NotificationsService(session))

        async def spend(amount: float) -> BudgetState:
            await usage.record(boris.user_id, "chat", "strong", cost_usd=amount)
            return (await budgets.check(boris.user_id)).state

        default = await budgets.set_limit(anna, boris.user_id, Decimal("10"))
        assert default.status.owner_alerts is OwnerAlerts.EXCEEDED
        assert await spend(8.5) is BudgetState.WARNING
        assert await owner_heard(session, anna.user_id) == []
        assert await spend(2.0) is BudgetState.EXCEEDED
        await budgets.check(boris.user_id)
        assert await owner_heard(session, anna.user_id) == [
            "exceeded: boris reached their monthly budget"
        ]

        await budgets.set_limit(anna, boris.user_id, Decimal("20"), owner_alerts=OwnerAlerts.ALL)
        assert await spend(6.0) is BudgetState.WARNING  # 16.50 of 20
        assert (await owner_heard(session, anna.user_id))[1:] == [
            "warning: boris used 80 % of their monthly budget"
        ]

        await budgets.set_limit(anna, boris.user_id, Decimal("20"), owner_alerts=OwnerAlerts.OFF)
        assert await spend(5.0) is BudgetState.EXCEEDED
        assert len(await owner_heard(session, anna.user_id)) == 2
        assert (await budget_notifications(session, boris.user_id))[-1] == "exceeded"

        # Leaving the setting out keeps it.
        kept = await budgets.set_limit(anna, boris.user_id, Decimal("21"))
        assert kept.status.owner_alerts is OwnerAlerts.OFF
        with pytest.raises(InvalidBudgetError):
            await budgets.set_limit(anna, boris.user_id, None, owner_alerts=OwnerAlerts.ALL)

        # An owner over their own limit hears about it once, not twice.
        await budgets.set_limit(anna, anna.user_id, Decimal("0"), owner_alerts=OwnerAlerts.ALL)
        own = await session.scalar(
            select(func.count()).where(
                Notification.user_id == anna.user_id,
                Notification.kind == NotificationKind.BUDGET,
                ~Notification.data.has_key("user_id"),
            )
        )
        assert own == 1
        assert len(await owner_heard(session, anna.user_id)) == 2
