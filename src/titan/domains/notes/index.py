"""Embedding notes and memories in the background (ADR 0014).

The worker's `embeddings` sweep calls `index_pending` on every tick. An item
needs embedding when it has no vectors of the current model, or when the hash
of its text no longer matches the one stored with them. Writes never wait for
this, and a failed server only delays it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from titan.domains.notes.models import Embedding, Memory, Note
from titan.embeddings import Embedder

CHUNK_CHARS = 1000
OVERLAP = 150
# Items looked at per tick, and the chunks embedded per tick. One item larger
# than the budget is embedded alone.
ITEM_BATCH = 20
CHUNK_BUDGET = 64
# Where a chunk may end, best first; within a tier the latest one wins.
_BREAKS = (("\n\n",), ("\n",), (". ", "! ", "? ", "; "), (" ",))


def split(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP) -> list[str]:
    """Pieces of at most `size` characters, cut at the best break in their second half."""
    text = text.strip()
    pieces: list[str] = []
    start = 0
    while len(text) - start > size:
        end = start + size
        cut = end
        for tier in _BREAKS:
            found, mark = max((text.rfind(mark, start + size // 2, end), mark) for mark in tier)
            if found != -1:
                cut = found + len(mark)
                break
        pieces.append(text[start:cut].strip())
        # The next piece repeats the end of this one, from a word start.
        back = text.find(" ", cut - overlap, cut)
        start = back + 1 if back != -1 else cut
    pieces.append(text[start:].strip())
    return [piece for piece in pieces if piece]


def note_chunks(title: str, body: str) -> list[str]:
    """Each chunk carries the title, so it stands on its own."""
    title = title.strip()
    if not body.strip():
        return [title]
    return [f"{title}\n\n{piece}" if title else piece for piece in split(body)]


def note_hash() -> ColumnElement[str]:
    return func.md5(Note.title + "\n" + Note.body)


def memory_hash() -> ColumnElement[str]:
    return func.md5(Memory.statement)


@dataclass(frozen=True)
class _Item:
    note_id: uuid.UUID | None
    memory_id: uuid.UUID | None
    content_hash: str
    texts: list[str]
    updated_at: datetime


async def _pending(session: AsyncSession, model: str) -> list[_Item]:
    """Items without current vectors, most recently changed first."""
    notes = await session.execute(
        select(Note.id, Note.title, Note.body, note_hash(), Note.updated_at)
        .where(
            ~exists().where(
                Embedding.note_id == Note.id,
                Embedding.model == model,
                Embedding.chunk_index == 0,
                Embedding.content_hash == note_hash(),
            )
        )
        .order_by(Note.updated_at.desc())
        .limit(ITEM_BATCH)
    )
    memories = await session.execute(
        select(Memory.id, Memory.statement, memory_hash(), Memory.updated_at)
        .where(
            ~exists().where(
                Embedding.memory_id == Memory.id,
                Embedding.model == model,
                Embedding.chunk_index == 0,
                Embedding.content_hash == memory_hash(),
            )
        )
        .order_by(Memory.updated_at.desc())
        .limit(ITEM_BATCH)
    )
    items = [
        _Item(note_id, None, digest, note_chunks(title, body), updated_at)
        for note_id, title, body, digest, updated_at in notes
    ] + [
        _Item(None, memory_id, digest, [statement], updated_at)
        for memory_id, statement, digest, updated_at in memories
    ]
    items.sort(key=lambda item: item.updated_at, reverse=True)
    return items[:ITEM_BATCH]


async def _replace(
    session: AsyncSession, model: str, item: _Item, vectors: list[list[float]]
) -> None:
    same_item = (
        Embedding.note_id == item.note_id
        if item.note_id is not None
        else Embedding.memory_id == item.memory_id
    )
    await session.execute(delete(Embedding).where(same_item, Embedding.model == model))
    session.add_all(
        Embedding(
            note_id=item.note_id,
            memory_id=item.memory_id,
            chunk_index=index,
            model=model,
            content_hash=item.content_hash,
            vector=vector,
        )
        for index, vector in enumerate(vectors)
    )
    await session.commit()


async def index_pending(sessions: async_sessionmaker[AsyncSession], embedder: Embedder) -> int:
    """Embed one batch of pending items; answers how many were stored.

    Raises EmbeddingsError when the server fails; nothing is stored then.
    """
    model = embedder.model
    async with sessions() as session:
        items = await _pending(session, model)
        if not items:
            # Everything has vectors of this model: those of earlier models go.
            await session.execute(delete(Embedding).where(Embedding.model != model))
            await session.commit()
            return 0
    batch: list[_Item] = []
    size = 0
    for item in items:
        if batch and size + len(item.texts) > CHUNK_BUDGET:
            break
        batch.append(item)
        size += len(item.texts)
    vectors = await embedder.embed([text for item in batch for text in item.texts])
    stored = 0
    offset = 0
    for item in batch:
        mine = vectors[offset : offset + len(item.texts)]
        offset += len(item.texts)
        # One transaction per item: one deleted meanwhile fails alone.
        async with sessions() as session:
            try:
                await _replace(session, model, item, mine)
            except IntegrityError:
                await session.rollback()
                continue
        stored += 1
    return stored
