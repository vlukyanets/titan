"""Notes, sharing, word search and memories in the domain service."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService
from titan.domains.chat.models import ChatThread
from titan.domains.notes.errors import (
    ForbiddenError,
    InvalidMemoryError,
    InvalidNoteError,
    NotFoundError,
)
from titan.domains.notes.models import MemorySource
from titan.domains.notes.service import (
    EXCERPT_LENGTH,
    MemoryService,
    NoteQuery,
    NotesService,
    words,
)

pytestmark = pytest.mark.db


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def user(sessions: async_sessionmaker[AsyncSession], username: str) -> uuid.UUID:
    async with sessions() as session:
        created = await AccountsService(session).create_user(
            username, "a-long-test-password", role=Role.MEMBER
        )
        return created.id


def test_only_the_first_words_of_a_query_count() -> None:
    assert words(None) == []
    assert words("  milk   bread ") == ["milk", "bread"]
    assert words(" ".join(str(i) for i in range(20))) == [str(i) for i in range(10)]


async def test_notes_are_normalized_listed_and_searched(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    boris = await user(sessions, "boris")
    async with sessions() as session:
        notes = NotesService(session)
        shopping = await notes.create_note(
            anna, title="  Shopping   list ", body="Купить МОЛОКО и хлеб", tags=["Home", "home"]
        )
        assert (shopping.note.title, shopping.note.tags) == ("Shopping list", ["home"])
        recipe = await notes.create_note(
            anna, body="Pancakes: 100% flour_mix, milk.\n\n" + "Stir well. " * 60, tags=["food"]
        )
        await notes.create_note(boris, title="Boris's milk")
        with pytest.raises(InvalidNoteError):
            await notes.create_note(anna, title=" ", body="\n")
        with pytest.raises(InvalidNoteError):
            await notes.create_note(anna, title="x", tags=["t" * 33])

        listed = await notes.notes(anna)
        assert [n.id for n in listed] == [recipe.note.id, shopping.note.id]
        excerpt = listed[0].excerpt
        assert excerpt.startswith("Pancakes: 100% flour_mix, milk. Stir well.")
        assert len(excerpt) == EXCERPT_LENGTH
        assert excerpt.endswith("…")
        assert listed[1].excerpt == "Купить МОЛОКО и хлеб"

        async def found(text: str | None = None, tag: str | None = None) -> list[uuid.UUID]:
            return [n.id for n in await notes.notes(anna, NoteQuery(text=text, tag=tag))]

        # Case is ignored in every alphabet, and a word may be part of a longer one.
        assert await found("молок") == [shopping.note.id]
        assert await found("ХЛЕБ купить") == [shopping.note.id]
        assert await found("MILK") == [recipe.note.id]
        assert await found("milk хлеб") == []
        assert await found("HOME list") == [shopping.note.id]
        assert await found("100%") == [recipe.note.id]
        assert await found("r_m") == [recipe.note.id]
        assert await found("0%f") == []
        assert await found(tag="FOOD") == [recipe.note.id]

        # A change moves a note to the top; paging follows the same order.
        await notes.update_note(anna, shopping.note.id, {"body": "Купить молоко, хлеб и сыр"})
        assert [n.id for n in await notes.notes(anna)] == [shopping.note.id, recipe.note.id]
        assert [n.id for n in await notes.notes(anna, limit=1)] == [shopping.note.id]
        rest = await notes.notes(anna, before=shopping.note.id)
        assert [n.id for n in rest] == [recipe.note.id]
        with pytest.raises(InvalidNoteError):
            await notes.notes(boris, before=shopping.note.id)


async def test_a_refused_update_changes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    async with sessions() as session:
        notes = NotesService(session)
        created = await notes.create_note(anna, title="Ideas", body="Plant tomatoes")
        for bad in (
            {"title": "", "body": ""},
            {"title": "New", "tags": [""]},
            {"title": "New", "owner_id": str(uuid.uuid4())},
            {"title": "New", "shared_with": [str(uuid.uuid4())]},
        ):
            with pytest.raises(InvalidNoteError):
                await notes.update_note(anna, created.note.id, bad)
    async with sessions() as session:
        again = await NotesService(session).get_note(anna, created.note.id)
        assert (again.note.title, again.note.body, again.shared_with) == (
            "Ideas",
            "Plant tomatoes",
            [],
        )
        emptied = await NotesService(session).update_note(anna, created.note.id, {"title": ""})
        assert (emptied.note.title, emptied.note.body) == ("", "Plant tomatoes")


async def test_shared_notes_are_read_only_for_readers(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    boris = await user(sessions, "boris")
    carl = await user(sessions, "carl")
    async with sessions() as session:
        notes = NotesService(session)
        wifi = await notes.create_note(
            anna, title="Wi-Fi at the dacha", body="fake-password-123", shared_with=[boris, anna]
        )
        assert wifi.shared_with == [boris]
        own = await notes.create_note(boris, title="Boris's own")

        assert [n.id for n in await notes.notes(boris)] == [own.note.id, wifi.note.id]
        assert [n.id for n in await notes.notes(boris, NoteQuery(mine=False))] == [wifi.note.id]
        assert [n.id for n in await notes.notes(boris, NoteQuery(mine=True))] == [own.note.id]
        assert [n.id for n in await notes.notes(boris, NoteQuery(text="dacha"))] == [wifi.note.id]
        shared = await notes.get_note(boris, wifi.note.id)
        assert (shared.note.owner_id, shared.shared_with) == (anna, [boris])
        with pytest.raises(ForbiddenError):
            await notes.update_note(boris, wifi.note.id, {"title": "Mine now"})
        with pytest.raises(ForbiddenError):
            await notes.delete_note(boris, wifi.note.id)
        for action in (
            notes.get_note(carl, wifi.note.id),
            notes.update_note(carl, wifi.note.id, {"title": "x"}),
            notes.delete_note(carl, wifi.note.id),
        ):
            with pytest.raises(NotFoundError):
                await action
        assert await notes.notes(carl) == []

        await notes.update_note(anna, wifi.note.id, {"shared_with": [carl]})
        with pytest.raises(NotFoundError):
            await notes.get_note(boris, wifi.note.id)
        assert (await notes.get_note(carl, wifi.note.id)).shared_with == [carl]
        await notes.delete_note(anna, wifi.note.id)
        with pytest.raises(NotFoundError):
            await notes.get_note(carl, wifi.note.id)


async def test_memories_are_confirmed_not_duplicated(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await user(sessions, "anna")
    boris = await user(sessions, "boris")
    async with sessions() as session:
        chat = ChatThread(user_id=anna, title="Dinner plans")
        others = ChatThread(user_id=boris)
        session.add_all([chat, others])
        await session.commit()
        notes = NotesService(session)
        shared = await notes.create_note(boris, title="Family", body="…", shared_with=[anna])
        private = await notes.create_note(boris, title="Private", body="…")

        memory = MemoryService(session)
        start = datetime(2026, 10, 1, 12, tzinfo=UTC)
        peanuts = await memory.remember(
            anna,
            "Anna is  allergic to peanuts",
            source=MemorySource.CHAT,
            source_id=chat.id,
            confidence=0.6,
            now=start,
        )
        assert (peanuts.statement, peanuts.confidence) == ("Anna is allergic to peanuts", 0.6)
        again = await memory.remember(
            anna,
            "anna is ALLERGIC to peanuts ",
            source=MemorySource.NOTE,
            source_id=shared.note.id,
            confidence=0.9,
            now=start + timedelta(days=3),
        )
        assert again.id == peanuts.id
        assert again.confidence == 0.9
        assert again.last_confirmed_at == start + timedelta(days=3)
        # The first source stays: that is where the fact was learned.
        assert (again.source, again.source_id) == (MemorySource.CHAT, chat.id)
        lower = await memory.remember(
            anna, "Anna is allergic to peanuts", source=MemorySource.USER, confidence=0.1
        )
        assert lower.confidence == 0.9
        coffee = await memory.remember(
            anna, "Пьёт кофе без мёда", source=MemorySource.NOTE, source_id=shared.note.id
        )
        assert await memory.remember(anna, "ПЬЁТ КОФЕ БЕЗ МЁДА", source=MemorySource.USER) == coffee

        for source, source_id in (
            (MemorySource.CHAT, others.id),
            (MemorySource.NOTE, private.note.id),
            (MemorySource.CHAT, None),
            (MemorySource.USER, chat.id),
        ):
            with pytest.raises(InvalidMemoryError):
                await memory.remember(anna, "Likes jazz", source=source, source_id=source_id)
        for statement, confidence in (("", 1.0), ("x" * 501, 1.0), ("Likes jazz", 1.5)):
            with pytest.raises(InvalidMemoryError):
                await memory.remember(
                    anna, statement, source=MemorySource.USER, confidence=confidence
                )

        assert [m.id for m in await memory.memories(anna)] == [coffee.id, peanuts.id]
        assert [m.id for m in await memory.memories(anna, text="кофе МЁД")] == [coffee.id]
        assert [m.id for m in await memory.memories(anna, before=coffee.id)] == [peanuts.id]
        assert await memory.memories(boris) == []
        with pytest.raises(NotFoundError):
            await memory.get_memory(boris, peanuts.id)
        with pytest.raises(NotFoundError):
            await memory.forget(boris, peanuts.id)

        edited = await memory.update_memory(
            anna, coffee.id, "Пьёт чай без мёда", now=start + timedelta(days=5)
        )
        assert (edited.confidence, edited.last_confirmed_at) == (1.0, start + timedelta(days=5))

        # A memory outlives the note it came from.
        await notes.delete_note(boris, shared.note.id)
        assert (await memory.get_memory(anna, coffee.id)).source_id == shared.note.id
        await memory.forget(anna, coffee.id)
        assert [m.id for m in await memory.memories(anna)] == [peanuts.id]
