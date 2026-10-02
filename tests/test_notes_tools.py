"""Agent tools of notes and memory: sharing asks first, memories confirm and undo."""

from __future__ import annotations

import uuid

import pytest

from tests.fake_claude import FakeClaude
from tests.test_agent_tools import World
from tests.test_agent_tools import world as world  # the fixture
from titan.agent.tools import REGISTRY, ToolScope, execute
from titan.domains.autonomy.models import ActionClass, Approval, AuditEntry
from titan.domains.autonomy.service import AuditService
from titan.domains.notes.models import Memory, MemorySource, Note
from titan.domains.notes.service import MemoryService, NotesService

pytestmark = pytest.mark.db

CREATE = "mcp__notes__create_note"
UPDATE = "mcp__notes__update_note"
SEARCH = "mcp__notes__search_notes"
REMEMBER = "mcp__memory__remember"
RECALL = "mcp__memory__recall"
REVISE = "mcp__memory__revise_memory"
FORGET = "mcp__memory__forget"


async def undo(world: World, entry_id: uuid.UUID) -> None:
    async with world.sessions() as session:
        await AuditService(session).undo(
            world.anna, entry_id, REGISTRY, world.scope().context(session)
        )


async def test_notes_are_saved_found_and_undone(world: World) -> None:
    fake = FakeClaude(
        [
            (CREATE, {"title": "Shopping", "body": "Молоко, хлеб", "tags": ["home"]}),
            (SEARCH, {"text": "молок"}),
        ]
    )
    await world.turn(fake)
    created, found = fake.outcomes
    assert not created.is_error
    assert "Shopping" in (found.result or "")
    assert "Молоко, хлеб" in (found.result or "")
    (entry,) = await world.rows(AuditEntry)
    await undo(world, entry.id)
    assert await world.rows(Note) == []


async def test_sharing_a_note_or_changing_a_shared_one_asks(world: World) -> None:
    async with world.sessions() as session:
        service = NotesService(session)
        private = await service.create_note(world.anna.user_id, title="Diary")
        shared = await service.create_note(
            world.anna.user_id, title="Trip", shared_with=[world.boris.user_id]
        )
    fake = FakeClaude(
        [
            (UPDATE, {"note_id": str(private.note.id), "shared_with": ["boris"]}),
            (UPDATE, {"note_id": str(shared.note.id), "body": "Day 1"}),
            (UPDATE, {"note_id": str(private.note.id), "body": "Dear diary"}),
        ]
    )
    await world.turn(fake)
    share, change_shared, change_private = fake.outcomes
    assert (share.decision, change_shared.decision, change_private.decision) == (
        "deny",
        "deny",
        "allow",
    )
    approvals = await world.rows(Approval)
    assert {(a.action_class, a.summary) for a in approvals} == {
        (ActionClass.EXTERNAL, "Change the note “Diary”: shared with boris"),
        (ActionClass.EXTERNAL, "Change the note “Trip”: body"),
    }
    (entry,) = await world.rows(AuditEntry)
    await undo(world, entry.id)
    async with world.sessions() as session:
        restored = await NotesService(session).get_note(world.anna.user_id, private.note.id)
    assert restored.note.body == ""


async def test_remember_confirms_known_facts_and_undo_restores(world: World) -> None:
    async with world.sessions() as session:
        known = await MemoryService(session).remember(
            world.anna.user_id,
            "Anna likes tea",
            source=MemorySource.USER,
            confidence=0.5,
        )
    fake = FakeClaude(
        [
            (REMEMBER, {"statement": "anna  likes TEA", "confidence": 0.9}),
            (REMEMBER, {"statement": "Anna is allergic to peanuts"}),
            (RECALL, {"text": "allergic"}),
        ]
    )
    await world.turn(fake)
    confirmed, new, recalled = fake.outcomes
    assert "Confirmed what I already knew" in (confirmed.result or "")
    assert "Remembered" in (new.result or "")
    assert "allergic to peanuts" in (recalled.result or "")
    memories = {m.statement: m for m in await world.rows(Memory)}
    assert memories["Anna likes tea"].confidence == 0.9
    assert memories["Anna is allergic to peanuts"].source is MemorySource.CHAT
    assert memories["Anna is allergic to peanuts"].source_id == world.thread.id

    for entry in sorted(await world.rows(AuditEntry), key=lambda e: e.id, reverse=True):
        await undo(world, entry.id)
    (left,) = await world.rows(Memory)
    assert (left.id, left.confidence) == (known.id, 0.5)


async def test_memory_needs_a_source_and_forgetting_asks(world: World) -> None:
    outside = ToolScope(world.sessions, world.anna.user_id)
    execution = await execute(REGISTRY[REMEMBER], outside, {"statement": "Likes jazz"})
    assert execution.result.is_error
    assert "needs the note" in execution.result.text

    async with world.sessions() as session:
        memory = await MemoryService(session).remember(
            world.anna.user_id, "Likes jazz", source=MemorySource.USER
        )
    fake = FakeClaude([(FORGET, {"memory_id": str(memory.id)})])
    await world.turn(fake)
    (outcome,) = fake.outcomes
    assert outcome.decision == "deny"
    assert "Forget “Likes jazz”" in outcome.reason
