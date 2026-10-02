# ruff: noqa: RUF001  (Cyrillic test data)
"""Search by words and by meaning, merged (docs/spec/domains/notes-memory.md#search)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.fake_embedder import FakeEmbedder
from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService
from titan.domains.notes.errors import InvalidMemoryError, InvalidNoteError
from titan.domains.notes.index import index_pending
from titan.domains.notes.models import MemorySource
from titan.domains.notes.service import MemoryService, NoteQuery, NotesService, fuse

# Unit vectors: CAR and NEAR_CAR are 0.05 apart in cosine distance, OTHER is 1 away.
CAR = [1.0, 0, 0, 0, 0, 0, 0, 0]
NEAR_CAR = [0.95, 0.312, 0, 0, 0, 0, 0, 0]
OTHER = [0, 1.0, 0, 0, 0, 0, 0, 0]


def test_fuse_ranks_items_found_both_ways_first() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert fuse([a, b], [b, c]) == [b, a, c]
    assert fuse([a, b]) == [a, b]
    assert fuse([], []) == []


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def user(sessions: async_sessionmaker[AsyncSession], name: str) -> uuid.UUID:
    async with sessions() as session:
        created = await AccountsService(session).create_user(
            name, "a-long-test-password", role=Role.MEMBER
        )
        return created.id


def embedder() -> FakeEmbedder:
    return FakeEmbedder(
        vectors={
            "car service": CAR,
            "ТО автомобиля\n\nЗаписаться на май": NEAR_CAR,
            "Car service plan\n\nRenew the gym": OTHER,
            "Борис: ТО автомобиля\n\nв июне": NEAR_CAR,
            "Анна водить стару машину": NEAR_CAR,
            "Boris drives a car": NEAR_CAR,
        }
    )


@pytest.mark.db
async def test_notes_are_found_by_meaning_and_by_words(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna, boris = await user(sessions, "anna"), await user(sessions, "boris")
    async with sessions() as session:
        notes = NotesService(session)
        car = (await notes.create_note(anna, title="ТО автомобиля", body="Записаться на май")).note
        gym = (await notes.create_note(anna, title="Car service plan", body="Renew the gym")).note
        await notes.create_note(boris, title="Борис: ТО автомобиля", body="в июне")
    fake = embedder()
    await index_pending(sessions, fake)

    async with sessions() as session:
        notes = NotesService(session, embedder=fake)
        found = await notes.notes(anna, NoteQuery(text="car service"))
        # One by meaning across languages, one by its words; Boris's note is
        # close too, but Anna cannot read it.
        assert {n.id for n in found} == {car.id, gym.id}
        assert fake.calls[-1] == (["car service"], True)
        assert [n.id for n in await notes.notes(anna, NoteQuery(text="flowers"))] == []
        with pytest.raises(InvalidNoteError):
            await notes.notes(anna, NoteQuery(text="car"), before=car.id)

    # Without the server, words only.
    fake.failing = True
    async with sessions() as session:
        found = await NotesService(session, embedder=fake).notes(anna, NoteQuery(text="service"))
        assert [n.id for n in found] == [gym.id]

    # Vectors of another model are not compared.
    other_model = embedder()
    other_model.model = "fake/model-b"
    async with sessions() as session:
        found = await NotesService(session, embedder=other_model).notes(
            anna, NoteQuery(text="car service")
        )
        assert [n.id for n in found] == [gym.id]


@pytest.mark.db
async def test_memories_are_found_by_meaning_only_by_their_owner(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna, boris = await user(sessions, "anna"), await user(sessions, "boris")
    async with sessions() as session:
        memory = MemoryService(session)
        old_car = await memory.remember(anna, "Анна водить стару машину", source=MemorySource.USER)
        await memory.remember(boris, "Boris drives a car", source=MemorySource.USER)
    fake = embedder()
    await index_pending(sessions, fake)

    async with sessions() as session:
        memory = MemoryService(session, embedder=fake)
        assert [m.id for m in await memory.memories(anna, text="car service")] == [old_car.id]
        assert await memory.memories(anna, text="flowers") == []
        with pytest.raises(InvalidMemoryError):
            await memory.memories(anna, text="car", before=old_car.id)
