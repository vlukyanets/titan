"""titan-worker: the scheduler loop.

On every tick the worker tries to hold each sweep's lease and runs the sweeps
it holds. Sweeps must be idempotent: across nodes they run at least once
(ADR 0006).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from titan.domains.notifications.service import NotificationsService
from titan.domains.reminders.service import RemindersService
from titan.notify import Pusher, UnifiedPushSender, new_client
from titan.scheduler import leases
from titan.settings import Settings
from titan.storage.db import create_engine, session_factory
from titan.storage.revision import check_revision

log = logging.getLogger(__name__)

# The most reminders one tick fires; the rest wait for the next tick.
REMINDER_BATCH = 100

Sweep = Callable[[datetime], Awaitable[int]]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Worker:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    pusher: Pusher | None = None
    clock: Callable[[], datetime] = _now
    sweeps: dict[str, Sweep] = field(init=False)

    def __post_init__(self) -> None:
        self.sweeps = {"reminders": self.fire_reminders}
        self.policy = leases.LeasePolicy(
            node=self.settings.node_name,
            preferred_node=self.settings.preferred_node or self.settings.node_name,
            ttl=timedelta(seconds=self.settings.scheduler_lease_seconds),
            grace=timedelta(seconds=self.settings.scheduler_grace_seconds),
        )

    async def fire_reminders(self, now: datetime) -> int:
        fired = 0
        async with self.sessions() as session:
            due = await RemindersService(session).due(now, limit=REMINDER_BATCH)
        for reminder_id in due:
            # One session per reminder: a failure stops only that one.
            async with self.sessions() as session:
                notifications = NotificationsService(
                    session,
                    pusher=self.pusher,
                    push_origins=self.settings.push_allowed_origins,
                )
                reminders = RemindersService(session, notifications=notifications)
                try:
                    if await reminders.fire(reminder_id, now=now) is not None:
                        fired += 1
                except Exception as exc:
                    await session.rollback()
                    log.error("reminder %s did not fire: %s", reminder_id, type(exc).__name__)
        return fired

    async def tick(self) -> dict[str, int]:
        """One round: the sweeps this node held the lease for, with what they did."""
        done: dict[str, int] = {}
        for name, sweep in self.sweeps.items():
            now = self.clock()
            async with self.sessions() as session:
                if not await leases.hold(session, name, self.policy, now):
                    continue
            done[name] = await sweep(now)
        return done

    async def run(self, stop: asyncio.Event) -> None:
        interval = self.settings.scheduler_tick_seconds
        log.info(
            "worker %s started; preferred node %s",
            self.policy.node,
            self.policy.preferred_node,
        )
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                # The database may be briefly unreachable; the next tick retries.
                log.error("scheduler tick failed: %s", type(exc).__name__)
            else:
                if self.settings.worker_heartbeat_file is not None:
                    self.settings.worker_heartbeat_file.touch()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
        log.info("worker %s stopped", self.policy.node)


async def serve(settings: Settings) -> None:
    engine = create_engine(settings)
    client = new_client(settings.push_timeout_seconds)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await check_revision(engine)
        await Worker(settings, session_factory(engine), UnifiedPushSender(client)).run(stop)
    finally:
        await client.aclose()
        await engine.dispose()


def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
