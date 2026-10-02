"""The trackers domain's agent tools: logging by name, undo, and exposure levels."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from tests.fake_claude import FakeClaude
from tests.test_agent_tools import World
from tests.test_agent_tools import world as world  # the fixture
from titan.agent.tools import REGISTRY, ToolScope, execute
from titan.domains.autonomy.errors import UndoConflictError
from titan.domains.autonomy.models import AuditEntry, Decision
from titan.domains.autonomy.service import AuditService
from titan.domains.autonomy.tools import Exposure
from titan.domains.trackers.models import Entry, Tracker
from titan.domains.trackers.service import TrackersService

pytestmark = pytest.mark.db

CREATE = "mcp__trackers__create_tracker"
UPDATE = "mcp__trackers__update_tracker"
LOG = "mcp__trackers__log_entry"
UPDATE_ENTRY = "mcp__trackers__update_entry"
ENTRIES = "mcp__trackers__list_entries"
STATS = "mcp__trackers__tracker_stats"
DELETE = "mcp__trackers__delete_tracker"


async def undo(world: World, entry_id: uuid.UUID) -> None:
    async with world.sessions() as session:
        await AuditService(session).undo(
            world.anna, entry_id, REGISTRY, world.scope().context(session)
        )


async def entries(world: World) -> list[Entry]:
    return await world.rows(Entry)


async def weight(world: World) -> Tracker:
    async with world.sessions() as session:
        return await TrackersService(session).create_tracker(
            world.anna.user_id, "Weight", template="weight"
        )


async def test_create_and_log_by_name_then_undo(world: World) -> None:
    fake = FakeClaude(
        [
            (CREATE, {"name": "Expenses", "template": "expense", "unit": "EUR"}),
            (LOG, {"tracker": "expenses", "value": "23.40", "category": "Groceries"}),
            (STATS, {"tracker": "Expenses"}),
        ]
    )
    await world.turn(fake)
    created, logged, stats = fake.outcomes
    assert (created.is_error, logged.is_error, stats.is_error) == (False, False, False)
    assert "Logged 23.4 EUR in “Expenses”" in (logged.result or "")
    assert "total 23.4 EUR in 1 entries" in (stats.result or "")
    assert "by category: groceries 23.4" in (stats.result or "")

    create_entry, log_entry = sorted(await world.rows(AuditEntry), key=lambda e: e.id)
    assert (log_entry.decision, log_entry.summary) == (
        Decision.AUTO_UNDO,
        "Log 23.40 in “Expenses”",
    )
    # The tracker cannot be undone while it has the entry; the entry can.
    with pytest.raises(UndoConflictError):
        await undo(world, create_entry.id)
    await undo(world, log_entry.id)
    assert await entries(world) == []
    await undo(world, create_entry.id)
    assert await world.rows(Tracker) == []


async def test_entry_and_tracker_changes_undo(world: World) -> None:
    tracker = await weight(world)
    async with world.sessions() as session:
        logged = await TrackersService(session).log(world.anna.user_id, tracker.id, "72.5")
    fake = FakeClaude(
        [
            (UPDATE_ENTRY, {"tracker": "Weight", "entry_id": str(logged.id), "value": 72.1}),
            (UPDATE, {"tracker": "Weight", "name": "Body weight", "archived": True}),
        ]
    )
    await world.turn(fake)
    assert [o.is_error for o in fake.outcomes] == [False, False]
    for entry in sorted(await world.rows(AuditEntry), key=lambda e: e.id, reverse=True):
        await undo(world, entry.id)
    (restored,) = await world.rows(Tracker)
    assert (restored.name, restored.archived) == ("Weight", False)
    (entry,) = await entries(world)
    assert entry.value == Decimal("72.5")


async def test_aggregates_exposure_hides_single_health_entries(world: World) -> None:
    tracker = await weight(world)
    async with world.sessions() as session:
        await TrackersService(session).log(world.anna.user_id, tracker.id, "72.5", note="after run")
    scope = ToolScope(world.sessions, world.anna.user_id, exposure=Exposure.AGGREGATES)
    listed = await execute(REGISTRY[ENTRIES], scope, {"tracker": "Weight"})
    assert listed.result.is_error
    assert "72.5" not in listed.result.text
    stats = await execute(REGISTRY[STATS], scope, {"tracker": "Weight"})
    assert not stats.result.is_error
    assert "total 72.5 kg" in stats.result.text
    assert "lowest" not in stats.result.text

    full = await execute(REGISTRY[ENTRIES], world.scope(), {"tracker": "Weight"})
    assert "72.5 kg “after run”" in full.result.text


async def test_deleting_a_tracker_asks_and_unknown_names_fail(world: World) -> None:
    await weight(world)
    fake = FakeClaude([(DELETE, {"tracker": "Weight"}), (LOG, {"tracker": "Sleep", "value": 7})])
    await world.turn(fake)
    deleted, unknown = fake.outcomes
    assert deleted.decision == "deny"
    assert "Delete the tracker “Weight” with all its entries" in deleted.reason
    assert unknown.is_error
    assert "no tracker called Sleep" in (unknown.result or "")
    assert len(await world.rows(Tracker)) == 1
