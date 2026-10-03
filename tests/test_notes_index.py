"""Background indexing of notes and memories, and the worker's sweep."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.fake_embedder import FakeEmbedder, hashed
from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService
from titan.domains.notes.index import index_pending
from titan.domains.notes.models import Embedding, MemorySource
from titan.domains.notes.service import MemoryService, NotesService
from titan.scheduler.worker import Worker
from titan.settings import Settings

pytestmark = pytest.mark.db


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def owner(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessions() as session:
        created = await AccountsService(session).create_user(
            "anna", "a-long-test-password", role=Role.MEMBER
        )
        return created.id


async def rows(sessions: async_sessionmaker[AsyncSession]) -> list[Embedding]:
    async with sessions() as session:
        result = await session.scalars(
            select(Embedding).order_by(Embedding.model, Embedding.chunk_index)
        )
        return list(result.all())


async def test_notes_and_memories_are_embedded_once_and_again_after_a_change(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await owner(sessions)
    async with sessions() as session:
        note = (
            await NotesService(session).create_note(anna, title="Car", body="Service in May")
        ).note
        memory = await MemoryService(session).remember(
            anna, "Anna is allergic to peanuts", source=MemorySource.USER
        )
    embedder = FakeEmbedder()
    assert await index_pending(sessions, embedder) == 2
    stored = await rows(sessions)
    assert {(row.note_id, row.memory_id) for row in stored} == {(note.id, None), (None, memory.id)}
    by_item = {row.note_id or row.memory_id: row for row in stored}
    assert list(by_item[note.id].vector) == pytest.approx(hashed("Car\n\nService in May"))
    assert list(by_item[memory.id].vector) == pytest.approx(hashed("Anna is allergic to peanuts"))
    assert embedder.calls[0][1] is False  # documents, not queries

    # Nothing changed: nothing to do.
    assert await index_pending(sessions, embedder) == 0
    assert len(embedder.calls) == 1

    async with sessions() as session:
        await NotesService(session).update_note(anna, note.id, {"body": "Service in June"})
    assert await index_pending(sessions, embedder) == 1
    [row] = [row for row in await rows(sessions) if row.note_id == note.id]
    assert list(row.vector) == pytest.approx(hashed("Car\n\nService in June"))


async def test_a_long_note_gets_one_row_per_chunk_and_they_go_with_it(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await owner(sessions)
    async with sessions() as session:
        note = (
            await NotesService(session).create_note(anna, title="Long", body="Sentence. " * 300)
        ).note
    assert await index_pending(sessions, FakeEmbedder()) == 1
    stored = await rows(sessions)
    assert [row.chunk_index for row in stored] == list(range(len(stored)))
    assert len(stored) >= 3
    assert len({row.content_hash for row in stored}) == 1
    async with sessions() as session:
        await NotesService(session).delete_note(anna, note.id)
    assert await rows(sessions) == []


async def test_a_new_model_replaces_the_old_vectors_once_it_is_done(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await owner(sessions)
    async with sessions() as session:
        for n in range(3):
            await MemoryService(session).remember(anna, f"fact {n}", source=MemorySource.USER)
    assert await index_pending(sessions, FakeEmbedder(model="fake/model-a")) == 3
    newer = FakeEmbedder(model="fake/model-b")
    assert await index_pending(sessions, newer) == 3
    # The old vectors stay until nothing is left for the new model...
    assert {row.model for row in await rows(sessions)} == {"fake/model-a", "fake/model-b"}
    assert await index_pending(sessions, newer) == 0
    # ...then they go.
    assert {row.model for row in await rows(sessions)} == {"fake/model-b"}


async def test_a_failing_server_stores_nothing_and_the_worker_carries_on(
    sessions: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    anna = await owner(sessions)
    async with sessions() as session:
        await MemoryService(session).remember(anna, "fact", source=MemorySource.USER)
    embedder = FakeEmbedder(failing=True)
    worker = Worker(Settings(database_url=db_url), sessions, embedder=embedder)
    # The tick only starts the batch; it runs as a task of its own.
    assert (await worker.tick())["embeddings"] == 1
    assert worker.indexing is not None
    assert await worker.indexing == 0
    assert await rows(sessions) == []
    embedder.failing = False
    assert (await worker.tick())["embeddings"] == 1
    assert await worker.indexing == 1
    assert len(await rows(sessions)) == 1


async def test_a_running_batch_is_not_started_twice(
    sessions: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    release = asyncio.Event()

    class SlowEmbedder(FakeEmbedder):
        async def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
            await release.wait()
            return await super().embed(texts, query=query)

    anna = await owner(sessions)
    async with sessions() as session:
        await MemoryService(session).remember(anna, "fact", source=MemorySource.USER)
    worker = Worker(Settings(database_url=db_url), sessions, embedder=SlowEmbedder())
    assert (await worker.tick())["embeddings"] == 1
    # Reminders do not wait for the batch.
    assert (await worker.tick())["embeddings"] == 0
    release.set()
    assert worker.indexing is not None
    assert await worker.indexing == 1


async def test_the_sweep_runs_only_with_an_embedder(db_url: str) -> None:
    engine = create_async_engine(db_url)
    try:
        worker = Worker(Settings(database_url=db_url), async_sessionmaker(engine))
        assert "embeddings" not in worker.sweeps
    finally:
        await engine.dispose()


async def test_an_item_deleted_while_it_is_embedded_is_skipped(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await owner(sessions)
    async with sessions() as session:
        gone = (await NotesService(session).create_note(anna, title="Gone")).note
        kept = (await NotesService(session).create_note(anna, title="Kept")).note

    class DeletingEmbedder(FakeEmbedder):
        async def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
            async with sessions() as session:
                await NotesService(session).delete_note(anna, gone.id)
            return await super().embed(texts, query=query)

    assert await index_pending(sessions, DeletingEmbedder()) == 1
    assert [row.note_id for row in await rows(sessions)] == [kept.id]
