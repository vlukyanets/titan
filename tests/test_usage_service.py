"""Usage records and monthly totals per user, model and household."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService, Principal
from titan.domains.usage.errors import ForbiddenError, InvalidMonthError
from titan.domains.usage.models import UsageRecord
from titan.domains.usage.service import UsageService, current_month, month_range


def test_months_are_utc_calendar_months() -> None:
    assert month_range("2026-09") == (
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert month_range("2026-12")[1] == datetime(2027, 1, 1, tzinfo=UTC)
    assert current_month(datetime(2026, 9, 24, 23, 0, tzinfo=UTC)) == "2026-09"
    for bad in ("2026-13", "2026-9", "09-2026", "", "2026-00"):
        with pytest.raises(InvalidMonthError):
            month_range(bad)


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def principal(
    sessions: async_sessionmaker[AsyncSession], username: str, role: Role = Role.MEMBER
) -> Principal:
    async with sessions() as session:
        user = await AccountsService(session).create_user(
            username, "a-long-test-password", role=role
        )
        return Principal(user.id, user.username, user.role, uuid.uuid4())


@pytest.mark.db
async def test_totals_add_up_per_model_and_month(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna", Role.OWNER)
    boris = await principal(sessions, "boris")
    async with sessions() as session:
        usage = UsageService(session)
        await usage.record(
            anna.user_id, "chat", "strong", input_tokens=100, output_tokens=20, cost_usd=0.01
        )
        await usage.record(anna.user_id, "chat", "strong", input_tokens=50, cache_read_tokens=900)
        await usage.record(anna.user_id, "chat", "fast", output_tokens=5, cost_usd=0.001)
        old = await usage.record(anna.user_id, "chat", "fast", input_tokens=7)
        await session.execute(
            update(UsageRecord)
            .where(UsageRecord.id == old.id)
            .values(created_at=datetime(2026, 8, 31, 23, 59, tzinfo=UTC))
        )
        await session.commit()
        month = current_month()
        mine = await usage.month(anna, month)
        assert mine.total.sessions == 3
        assert (mine.total.input_tokens, mine.total.output_tokens) == (150, 25)
        assert mine.total.cache_read_tokens == 900
        assert mine.total.cost_usd == pytest.approx(0.011)
        assert set(mine.by_model) == {"fast", "strong"}
        assert mine.by_model["strong"].sessions == 2
        assert (await usage.month(anna, "2026-08")).total.input_tokens == 7
        assert (await usage.month(boris, month)).total.sessions == 0

        household = await usage.household(anna, month)
        assert [(m.username, m.usage.total.sessions) for m in household] == [
            ("anna", 3),
            ("boris", 0),
        ]
        assert [m.username for m in await usage.household(None, month)] == ["anna", "boris"]
        with pytest.raises(ForbiddenError):
            await usage.household(boris, month)
