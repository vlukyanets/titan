"""titan-worker: the scheduler loop.

On every tick the worker tries to hold each sweep's lease and runs the sweeps
it holds. Sweeps must be idempotent: across nodes they run at least once
(ADR 0006).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from titan import embeddings
from titan.agent.workflows import Due, Workflows
from titan.domains.notes.index import index_pending
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
PUSH_BATCH = 100

Sweep = Callable[[datetime], Awaitable[int]]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Worker:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    pusher: Pusher | None = None
    clock: Callable[[], datetime] = _now
    # Agent workflows; None in tests that only fire reminders.
    workflows: Workflows | None = None
    # Semantic search (ADR 0014); None when it is off.
    embedder: embeddings.Embedder | None = None
    sweeps: dict[str, Sweep] = field(init=False)
    # Daily plans run as tasks of their own, so a slow agent session never holds
    # up the reminders of the next tick.
    plans: dict[Due, asyncio.Task[bool]] = field(init=False, default_factory=dict)
    # Indexing too: on two cores one batch of embeddings takes up to a minute.
    indexing: asyncio.Task[int] | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.sweeps = {"reminders": self.fire_reminders, "push_retries": self.retry_pushes}
        if self.workflows is not None:
            self.sweeps |= {"replan_blocks": self.replan_blocks, "daily_plan": self.start_plans}
        if self.embedder is not None:
            self.sweeps["embeddings"] = self.index_embeddings
        self._embeddings_down = False
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
                reminders = RemindersService(session, notifications=self._notifications(session))
                try:
                    if await reminders.fire(reminder_id, now=now) is not None:
                        fired += 1
                except Exception as exc:
                    await session.rollback()
                    log.error("reminder %s did not fire: %s", reminder_id, type(exc).__name__)
        return fired

    def _notifications(self, session: AsyncSession) -> NotificationsService:
        return NotificationsService(
            session, pusher=self.pusher, push_origins=self.settings.push_allowed_origins
        )

    async def retry_pushes(self, now: datetime) -> int:
        """Push notifications again that no device accepted; answers how many got through."""
        delivered = 0
        async with self.sessions() as session:
            due = await self._notifications(session).retries_due(now, limit=PUSH_BATCH)
        for notification_id in due:
            async with self.sessions() as session:
                try:
                    delivered += bool(
                        await self._notifications(session).retry(notification_id, now)
                    )
                except Exception as exc:
                    await session.rollback()
                    log.error("push retry %s failed: %s", notification_id, type(exc).__name__)
        return delivered

    async def replan_blocks(self, now: datetime) -> int:
        assert self.workflows is not None
        return await self.workflows.replan_missed(now)

    async def start_plans(self, now: datetime) -> int:
        """Start the daily plans that are due and not running yet."""
        workflows = self.workflows
        if workflows is None or not await workflows.ready():
            return 0
        started = 0
        for item in await workflows.due_daily_plans(now):
            if item in self.plans:
                continue
            task = asyncio.create_task(workflows.daily_plan(item), name=f"daily-plan-{item.day}")
            self.plans[item] = task
            task.add_done_callback(functools.partial(self._plan_done, item))
            started += 1
        return started

    def _plan_done(self, item: Due, _: asyncio.Task[bool]) -> None:
        self.plans.pop(item, None)

    async def index_embeddings(self, now: datetime) -> int:
        """Start a batch of indexing unless one is running; answers 1 if it started."""
        if self.indexing is not None and not self.indexing.done():
            return 0
        self.indexing = asyncio.create_task(self._index(), name="embeddings")
        return 1

    async def _index(self) -> int:
        assert self.embedder is not None
        try:
            stored = await index_pending(self.sessions, self.embedder)
        except embeddings.EmbeddingsError as exc:
            # Logged once per outage: the server may be loading its model or gone.
            if not self._embeddings_down:
                log.warning("embeddings are not indexed: %s", exc)
            self._embeddings_down = True
            return 0
        except Exception as exc:
            log.error("embedding failed: %s", type(exc).__name__)
            return 0
        if self._embeddings_down:
            log.info("embeddings server is back")
        self._embeddings_down = False
        return stored

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
        running = [*self.plans.values(), *([self.indexing] if self.indexing else [])]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        log.info("worker %s stopped", self.policy.node)


async def serve(settings: Settings) -> None:
    engine = create_engine(settings)
    client = new_client(settings.push_timeout_seconds)
    embeddings_client = httpx.AsyncClient()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await check_revision(engine)
        sessions = session_factory(engine)
        pusher = UnifiedPushSender(client)
        workflows = Workflows(settings, sessions, pusher=pusher)
        embedder = embeddings.from_settings(settings, embeddings_client)
        await Worker(settings, sessions, pusher, workflows=workflows, embedder=embedder).run(stop)
    finally:
        await embeddings_client.aclose()
        await client.aclose()
        await engine.dispose()


def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
