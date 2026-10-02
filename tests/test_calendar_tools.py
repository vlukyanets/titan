"""Calendar and planner tools: invitations ask first, plans fit, undo removes them."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.fake_claude import FakeClaude
from tests.test_agent_tools import World
from tests.test_agent_tools import world as world  # the fixture
from titan.agent.tools import REGISTRY, execute
from titan.domains.autonomy.models import ActionClass, Approval, AuditEntry
from titan.domains.autonomy.service import AuditService
from titan.domains.calendar.models import Event, EventKind
from titan.domains.calendar.service import CalendarService
from titan.domains.tasks.service import TasksService

pytestmark = pytest.mark.db

KYIV = ZoneInfo("Europe/Kyiv")
CREATE = "mcp__calendar__create_event"
UPDATE = "mcp__calendar__update_event"
PLAN = "mcp__calendar__plan_day"
REPLAN = "mcp__calendar__replan_block"
LIST = "mcp__calendar__list_events"


def next_monday() -> date:
    today = datetime.now(KYIV).date()
    return today + timedelta(days=7 - today.weekday())


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), KYIV)


async def undo(world: World, entry_id: uuid.UUID) -> None:
    async with world.sessions() as session:
        await AuditService(session).undo(
            world.anna, entry_id, REGISTRY, world.scope().context(session)
        )


async def kyiv_prefs(world: World) -> None:
    async with world.sessions() as session:
        await CalendarService(session).set_prefs(
            world.anna.user_id,
            time_zone="Europe/Kyiv",
            work_start=time(9),
            work_end=time(17),
            work_days=[1, 2, 3, 4, 5],
            buffer_minutes=10,
        )


async def test_inviting_asks_and_own_events_are_undone(world: World) -> None:
    await kyiv_prefs(world)
    day = next_monday()
    fake = FakeClaude(
        [
            (
                CREATE,
                {
                    "title": "Dinner",
                    "starts_at": f"{day}T19:00",
                    "ends_at": f"{day}T20:00",
                    "attendees": ["boris"],
                },
            ),
            (CREATE, {"title": "Gym", "starts_at": f"{day}T18:00", "ends_at": f"{day}T19:00"}),
            (LIST, {"start": str(day), "end": str(day + timedelta(days=1))}),
        ]
    )
    await world.turn(fake)
    invite, gym, listed = fake.outcomes
    assert (invite.decision, gym.decision) == ("deny", "allow")
    (approval,) = await world.rows(Approval)
    assert approval.action_class is ActionClass.EXTERNAL
    assert f"{day:%a %Y-%m-%d} 18:00 to 19:00 Gym" in (listed.result or "")
    (entry,) = await world.rows(AuditEntry)
    await undo(world, entry.id)
    assert await world.rows(Event) == []


async def test_plan_day_fits_around_events_and_undo_removes_it(world: World) -> None:
    await kyiv_prefs(world)
    day = next_monday()
    async with world.sessions() as session:
        await CalendarService(session).create_event(
            world.anna.user_id, "Standup", at(day, 9), at(day, 10)
        )
        tasks = TasksService(session)
        report = await tasks.create_task(
            world.anna.user_id,
            "Report",
            estimate_minutes=120,
            priority=1,
            due_at=at(day, 18),
        )
        await tasks.create_task(world.anna.user_id, "Email", priority=2)
        await tasks.create_task(world.anna.user_id, "Huge", estimate_minutes=600)
    fake = FakeClaude([(PLAN, {"date": str(day)})])
    await world.turn(fake)
    (outcome,) = fake.outcomes
    assert not outcome.is_error
    assert "No room for: Huge" in (outcome.result or "")
    blocks = sorted(
        await world.rows(Event, Event.kind == EventKind.TIME_BLOCK), key=lambda e: e.starts_at
    )
    # The report goes first (it is due), after the standup and its buffer.
    assert [(b.title, b.starts_at, b.ends_at) for b in blocks] == [
        ("Report", at(day, 10, 10), at(day, 12, 10)),
        ("Email", at(day, 12, 20), at(day, 12, 50)),
    ]
    assert blocks[0].task_id == report.id

    # Planning again skips tasks that already have a block that day.
    again = await execute(REGISTRY[PLAN], world.scope(), {"date": str(day)})
    assert "No room for: Huge" in again.result.text
    assert len(await world.rows(Event, Event.kind == EventKind.TIME_BLOCK)) == 2

    first = min(await world.rows(AuditEntry), key=lambda e: e.id)
    await undo(world, first.id)
    assert await world.rows(Event, Event.kind == EventKind.TIME_BLOCK) == []


async def test_replan_moves_a_missed_block_to_the_next_free_slot(world: World) -> None:
    await kyiv_prefs(world)
    now = datetime.now(UTC)
    async with world.sessions() as session:
        task = await TasksService(session).create_task(world.anna.user_id, "Taxes")
        block = await CalendarService(session).create_event(
            world.anna.user_id,
            "Taxes",
            now - timedelta(hours=2),
            now - timedelta(hours=1),
            kind=EventKind.TIME_BLOCK,
            task_id=task.id,
        )
    fake = FakeClaude([(REPLAN, {"event_id": str(block.event.id)})])
    await world.turn(fake)
    (outcome,) = fake.outcomes
    assert not outcome.is_error, outcome.result
    (moved,) = await world.rows(Event)
    assert moved.starts_at >= now + timedelta(minutes=10)
    assert moved.ends_at - moved.starts_at == timedelta(hours=1)
    local = moved.starts_at.astimezone(KYIV)
    assert local.isoweekday() <= 5
    assert local.time() >= time(9)
    assert moved.ends_at.astimezone(KYIV).time() <= time(17)
    (entry,) = await world.rows(AuditEntry)
    await undo(world, entry.id)
    (back,) = await world.rows(Event)
    assert back.starts_at == block.event.starts_at
