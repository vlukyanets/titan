"""Scheduled workflows: the daily plan, the budget check of the runner, replanning."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import ClaudeAgentOptions, Message

from tests.fake_claude import FakeClaude
from tests.test_agent_tools import World
from tests.test_agent_tools import world as world  # the fixture
from titan.agent.workflows import DAILY_PLAN_TOOLS, Due, Workflows
from titan.domains.autonomy.models import ActionClass, Approval, Decision
from titan.domains.autonomy.service import PolicyService
from titan.domains.calendar.models import Event, EventKind
from titan.domains.calendar.service import CalendarService
from titan.domains.notifications.models import Notification, NotificationKind
from titan.domains.tasks.service import TasksService
from titan.domains.usage.budget import BudgetService
from titan.domains.usage.models import UsageRecord
from titan.domains.usage.service import UsageService
from titan.scheduler.worker import Worker

pytestmark = pytest.mark.db

KYIV = ZoneInfo("Europe/Kyiv")
PLAN = "mcp__calendar__plan_day"


def next_monday() -> date:
    today = datetime.now(KYIV).date()
    return today + timedelta(days=7 - today.weekday())


def workflows(world: World, fake: Any) -> Workflows:
    return Workflows(
        world.settings,
        world.sessions,
        pusher=world.pusher,
        query_fn=fake,
        environ=dict(world.environ),
    )


async def setup_anna(world: World) -> None:
    async with world.sessions() as session:
        await CalendarService(session).set_prefs(
            world.anna.user_id,
            time_zone="Europe/Kyiv",
            work_start=time(9),
            work_end=time(17),
            work_days=[1, 2, 3, 4, 5],
            buffer_minutes=10,
            daily_plan_at=time(7),
        )
        await TasksService(session).create_task(world.anna.user_id, "Report", estimate_minutes=60)


async def test_the_daily_plan_runs_once_and_sends_its_summary(world: World) -> None:
    await setup_anna(world)
    day = next_monday()
    early = datetime.combine(day, time(6, 59), KYIV)
    fake = FakeClaude([(PLAN, {"date": str(day)})], reply="Today: Report at 09:00.")
    flows = workflows(world, fake)
    assert await flows.due_daily_plans(early) == []
    # Boris has no open tasks, so only Anna is due.
    (due,) = await flows.due_daily_plans(early + timedelta(minutes=1))
    assert due == Due(world.anna.user_id, day)

    assert await flows.daily_plan(due)
    servers = fake.options[0].mcp_servers
    assert isinstance(servers, dict)
    assert set(servers) == {"tasks", "calendar", "reminders", "memory"}
    (planned,) = fake.outcomes
    assert planned.decision == "allow"
    (block,) = await world.rows(Event, Event.kind == EventKind.TIME_BLOCK)
    assert block.starts_at == datetime.combine(day, time(9), KYIV)
    (note,) = await world.rows(Notification, Notification.id == due.run_id)
    assert (note.kind, note.body) == (NotificationKind.PLAN, "Today: Report at 09:00.")
    (usage,) = await world.rows(UsageRecord)
    assert (usage.source, usage.reference) == ("daily_plan", due.run_id)

    assert await flows.due_daily_plans(early + timedelta(hours=1)) == []
    assert not await flows.daily_plan(due)
    # After working hours, or with the plan turned off, nothing is due.
    assert (
        await flows.due_daily_plans(datetime.combine(day, time(17), KYIV) + timedelta(days=1)) == []
    )


async def test_over_budget_the_run_is_skipped_and_the_user_told(world: World) -> None:
    await setup_anna(world)
    async with world.sessions() as session:
        await UsageService(session).record(
            world.anna.user_id, "chat", "m", input_tokens=1, output_tokens=1, cost_usd=5.0
        )
        await BudgetService(session).set_limit(None, world.anna.user_id, Decimal(1))
    fake = FakeClaude([])
    flows = workflows(world, fake)
    due = Due(world.anna.user_id, next_monday())
    assert not await flows.daily_plan(due)
    assert fake.options == []
    skipped = await world.rows(Notification, Notification.id == due.run_id)
    assert [(n.kind, n.data) for n in skipped] == [
        (NotificationKind.BUDGET, {"workflow": "daily_plan"})
    ]


async def test_a_failed_run_says_so_once(world: World) -> None:
    await setup_anna(world)

    def broken(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        raise RuntimeError("no Claude today")

    flows = workflows(world, broken)
    due = Due(world.anna.user_id, next_monday())
    assert await flows.daily_plan(due)
    (note,) = await world.rows(Notification, Notification.id == due.run_id)
    assert note.data["failed"] is True
    assert not await flows.daily_plan(due)


async def missed_block(world: World, title: str) -> Event:
    now = datetime.now(UTC)
    async with world.sessions() as session:
        task = await TasksService(session).create_task(world.anna.user_id, title)
        shared = await CalendarService(session).create_event(
            world.anna.user_id,
            title,
            now - timedelta(minutes=50),
            now - timedelta(minutes=20),
            kind=EventKind.TIME_BLOCK,
            task_id=task.id,
        )
    return shared.event


async def test_missed_blocks_move_by_themselves_or_ask(world: World) -> None:
    flows = workflows(world, FakeClaude([]))
    block = await missed_block(world, "Taxes")
    assert await flows.replan_missed(datetime.now(UTC)) == 1
    (moved,) = await world.rows(Event, Event.id == block.id)
    assert moved.starts_at > datetime.now(UTC)
    (told,) = await world.rows(Notification, Notification.kind == NotificationKind.PLAN)
    assert told.body.startswith("Moved “Taxes” to")
    assert await flows.replan_missed(datetime.now(UTC)) == 0

    async with world.sessions() as session:
        await PolicyService(session).set_rule(
            world.anna, "calendar", ActionClass.WRITE_INTERNAL, Decision.CONFIRM
        )
    other = await missed_block(world, "Gym")
    assert await flows.replan_missed(datetime.now(UTC)) == 1
    (approval,) = await world.rows(Approval)
    assert approval.input == {"event_id": str(other.id)}
    assert approval.summary == "Move the missed block “Gym” to the next free slot"
    assert await flows.replan_missed(datetime.now(UTC)) == 0


def test_the_daily_plan_uses_only_known_tools() -> None:
    from titan.agent.tools import REGISTRY

    assert set(REGISTRY) >= DAILY_PLAN_TOOLS


async def test_the_worker_starts_due_plans_in_the_background(world: World) -> None:
    await setup_anna(world)
    flows = workflows(world, FakeClaude([], reply="Nothing urgent today."))
    worker = Worker(world.settings, world.sessions, world.pusher, workflows=flows)
    now = datetime.combine(next_monday(), time(7, 30), KYIV)
    assert await worker.start_plans(now) == 1
    # Still running or done, it is not started twice.
    assert await worker.start_plans(now) == 0
    await asyncio.gather(*worker.plans.values())
    assert worker.plans == {}
    notes = await world.rows(Notification, Notification.kind == NotificationKind.PLAN)
    assert [n.body for n in notes] == ["Nothing urgent today."]
