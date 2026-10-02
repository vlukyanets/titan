"""Agent tools of reminders: create, change and snooze with undo."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.fake_claude import FakeClaude
from tests.test_agent_tools import World
from tests.test_agent_tools import world as world  # the fixture
from titan.agent.tools import REGISTRY
from titan.domains.autonomy.errors import UndoConflictError
from titan.domains.autonomy.models import AuditEntry
from titan.domains.autonomy.service import AuditService
from titan.domains.reminders.models import Reminder, ReminderStatus
from titan.domains.reminders.service import RemindersService

pytestmark = pytest.mark.db

CREATE = "mcp__reminders__create_reminder"
UPDATE = "mcp__reminders__update_reminder"
SNOOZE = "mcp__reminders__snooze_reminder"
DELETE = "mcp__reminders__delete_reminder"


async def undo(world: World, entry_id: uuid.UUID) -> None:
    async with world.sessions() as session:
        await AuditService(session).undo(
            world.anna, entry_id, REGISTRY, world.scope().context(session)
        )


async def test_create_change_and_undo(world: World) -> None:
    fire_at = (datetime.now(UTC) + timedelta(days=1)).replace(second=0, microsecond=0)
    fake = FakeClaude([(CREATE, {"text": "Pills", "fire_at": fire_at.isoformat()})])
    await world.turn(fake)
    (created,) = fake.outcomes
    assert "I will remind you" in (created.result or "")
    (reminder,) = await world.rows(Reminder)
    assert reminder.fire_at == fire_at

    fake = FakeClaude([(UPDATE, {"reminder_id": str(reminder.id), "recurrence": "FREQ=DAILY"})])
    await world.turn(fake)
    entries = sorted(await world.rows(AuditEntry), key=lambda e: e.id)
    assert entries[1].summary == "Change the reminder “Pills”: recurrence"
    await undo(world, entries[1].id)
    (restored,) = await world.rows(Reminder)
    assert restored.recurrence is None
    await undo(world, entries[0].id)
    assert await world.rows(Reminder) == []


async def test_snoozing_a_series_makes_a_copy_that_undo_removes(world: World) -> None:
    now = datetime.now(UTC)
    async with world.sessions() as session:
        reminders = RemindersService(session)
        daily = await reminders.create(
            world.anna.user_id, "Water", now - timedelta(minutes=1), recurrence="FREQ=DAILY"
        )
        await reminders.fire(daily.id, now=now)
        once = await reminders.create(world.anna.user_id, "Call", now + timedelta(hours=1))
    fake = FakeClaude(
        [
            (SNOOZE, {"reminder_id": str(daily.id), "minutes": 30}),
            (SNOOZE, {"reminder_id": str(once.id)}),
        ]
    )
    await world.turn(fake)
    assert len(await world.rows(Reminder)) == 3
    copy_entry, once_entry = sorted(await world.rows(AuditEntry), key=lambda e: e.id)
    await undo(world, copy_entry.id)
    await undo(world, once_entry.id)
    left = {r.text: r for r in await world.rows(Reminder)}
    assert set(left) == {"Water", "Call"}
    assert left["Call"].status is ReminderStatus.SCHEDULED
    assert left["Call"].fire_at == once.fire_at


async def test_undo_after_the_reminder_fired_conflicts(world: World) -> None:
    soon = datetime.now(UTC) + timedelta(minutes=1)
    fake = FakeClaude([(CREATE, {"text": "Tea", "fire_at": soon.isoformat()})])
    await world.turn(fake)
    (entry,) = await world.rows(AuditEntry)
    (reminder,) = await world.rows(Reminder)
    async with world.sessions() as session:
        await RemindersService(session).fire(reminder.id, now=soon + timedelta(seconds=1))
    with pytest.raises(UndoConflictError):
        await undo(world, entry.id)
    fake = FakeClaude([(DELETE, {"reminder_id": str(reminder.id)})])
    await world.turn(fake)
    (outcome,) = fake.outcomes
    assert outcome.decision == "deny"
    assert "Delete the reminder “Tea”" in outcome.reason
