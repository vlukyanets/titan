"""Sweep leases with a preferred node, the worker's tick and the startup check."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService
from titan.domains.notifications.models import Notification
from titan.domains.reminders.models import Reminder, ReminderStatus
from titan.domains.reminders.service import RemindersService
from titan.scheduler.leases import LeasePolicy, hold
from titan.scheduler.worker import Worker
from titan.settings import Settings
from titan.storage.revision import SchemaTooOldError, check_revision, is_older

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=30)
GRACE = timedelta(seconds=30)
HOME = LeasePolicy(node="home", preferred_node="home", ttl=TTL, grace=GRACE)
LAPTOP = LeasePolicy(node="laptop", preferred_node="home", ttl=TTL, grace=GRACE)


def test_only_a_known_older_revision_is_refused() -> None:
    assert is_older(None)
    assert is_older("e194cfa16ecf")  # the first revision
    assert not is_older("0123456789ab")  # unknown to this code: newer, compatible


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.db
async def test_the_startup_check_accepts_a_migrated_database(db_url: str) -> None:
    engine = create_async_engine(db_url)
    try:
        await check_revision(engine)
    finally:
        await engine.dispose()
    empty = create_async_engine(db_url.rsplit("/", 1)[0] + "/postgres")
    try:
        with pytest.raises(SchemaTooOldError, match="titan migrate"):
            await check_revision(empty)
    finally:
        await empty.dispose()


@pytest.mark.db
async def test_the_preferred_node_gets_the_lease_back(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async def tick(policy: LeasePolicy, seconds: float) -> bool:
        async with sessions() as session:
            return await hold(session, "reminders", policy, T0 + timedelta(seconds=seconds))

    # The laptop starts first and takes the free lease.
    assert await tick(LAPTOP, 0)
    assert await tick(LAPTOP, 3)
    # The home server comes up: it waits for the laptop's lease, which the
    # laptop hands back as soon as it sees the home server.
    assert not await tick(HOME, 4)
    assert not await tick(LAPTOP, 5)
    assert await tick(HOME, 6)
    assert not await tick(LAPTOP, 10)
    assert await tick(HOME, 11)

    # The home server drops off at 11s; its lease runs to 41s. The laptop waits
    # for the expiry plus the grace period.
    assert not await tick(LAPTOP, 45)
    assert not await tick(LAPTOP, 70)
    assert await tick(LAPTOP, 72)
    # Back again: the home server gets the lease within one period.
    assert not await tick(HOME, 80)
    assert not await tick(LAPTOP, 82)
    assert await tick(HOME, 83)


async def reminder_due(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessions() as session:
        user = await AccountsService(session).create_user(
            "anna", "a-long-test-password", role=Role.MEMBER
        )
        reminder = await RemindersService(session).create(user.id, "Water the plants", T0)
        return reminder.id


@pytest.mark.db
async def test_the_lease_holder_fires_due_reminders(
    db_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    reminder_id = await reminder_due(sessions)
    now = T0 + timedelta(seconds=2)

    def worker(node: str) -> Worker:
        settings = Settings(database_url=db_url, node_name=node, preferred_node="home")
        return Worker(settings, sessions, clock=lambda: now)

    home, laptop = worker("home"), worker("laptop")
    assert await home.tick() == {"reminders": 1}
    assert await laptop.tick() == {}
    assert await home.tick() == {"reminders": 0}

    async with sessions() as session:
        reminder = await session.get(Reminder, reminder_id)
        assert reminder is not None
        assert reminder.status is ReminderStatus.FIRED
        assert await session.scalar(select(func.count()).select_from(Notification)) == 1


@pytest.mark.db
async def test_the_worker_beats_and_stops_when_asked(
    db_url: str, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    heartbeat = tmp_path / "alive"
    settings = Settings(
        database_url=db_url,
        node_name="home",
        scheduler_tick_seconds=60,
        worker_heartbeat_file=heartbeat,
    )
    stop = asyncio.Event()
    run = asyncio.create_task(Worker(settings, sessions).run(stop))
    for _ in range(50):
        if heartbeat.exists():
            break
        await asyncio.sleep(0.1)
    assert heartbeat.exists()
    stop.set()
    await asyncio.wait_for(run, timeout=5)
