"""Notes and memories (docs/spec/domains/notes-memory.md)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from titan.api.deps import CurrentPrincipal, Session
from titan.api.problems import PROBLEM_JSON
from titan.domains.notes.models import Memory, MemorySource
from titan.domains.notes.service import (
    BODY_LENGTH,
    DEFAULT_PAGE,
    MAX_PAGE,
    MAX_SHARED,
    MAX_TAGS,
    QUERY_LENGTH,
    STATEMENT_LENGTH,
    TAG_LENGTH,
    TITLE_LENGTH,
    MemoryService,
    NoteQuery,
    NotesService,
    NoteSummary,
    SharedNote,
)

notes_router = APIRouter(prefix="/notes", tags=["notes"])
memories_router = APIRouter(prefix="/memories", tags=["notes"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}
_WORDS = "Every word must appear, also inside longer words; case is ignored"


def get_notes(session: Session) -> NotesService:
    return NotesService(session)


def get_memory(session: Session) -> MemoryService:
    return MemoryService(session)


Notes = Annotated[NotesService, Depends(get_notes)]
Memories = Annotated[MemoryService, Depends(get_memory)]


class NoteScope(enum.StrEnum):
    MINE = "mine"
    WITH_ME = "with_me"


# ---------------------------------------------------------------- notes


class NoteOut(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    body: str = Field(description="Markdown")
    tags: list[str]
    shared_with: list[uuid.UUID] = Field(description="Users besides the owner who can read it")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, shared: SharedNote) -> NoteOut:
        n = shared.note
        return cls(
            id=n.id,
            owner_id=n.owner_id,
            title=n.title,
            body=n.body,
            tags=list(n.tags),
            shared_with=shared.shared_with,
            created_at=n.created_at,
            updated_at=n.updated_at,
        )


class NoteSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    excerpt: str = Field(description="The start of the body on one line")
    tags: list[str]
    shared_with: list[uuid.UUID]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, summary: NoteSummary) -> NoteSummaryOut:
        return cls.model_validate(summary)


class NoteCreate(BaseModel):
    """A note needs a title or a body."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=TITLE_LENGTH)
    body: str = Field(default="", max_length=BODY_LENGTH, description="Markdown")
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    shared_with: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SHARED)


class NotePatch(BaseModel):
    """Only the fields sent change. Owner only."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=TITLE_LENGTH)
    body: str | None = Field(default=None, max_length=BODY_LENGTH)
    tags: list[str] | None = Field(default=None, max_length=MAX_TAGS)
    shared_with: list[uuid.UUID] | None = Field(
        default=None, max_length=MAX_SHARED, description="Replaces the whole list"
    )


@notes_router.get(
    "",
    summary="Notes the caller owns or that are shared with them, most recently changed first",
    description="Page with `before`: pass the id of the last note you have.",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def list_notes(
    principal: CurrentPrincipal,
    notes: Notes,
    q: Annotated[str | None, Query(max_length=QUERY_LENGTH, description=_WORDS)] = None,
    tag: Annotated[str | None, Query(max_length=TAG_LENGTH)] = None,
    shared: Annotated[
        NoteScope | None, Query(description="Only the caller's notes, or only others' shared")
    ] = None,
    before: Annotated[uuid.UUID | None, Query(description="Only after this note")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> list[NoteSummaryOut]:
    mine = None if shared is None else shared is NoteScope.MINE
    query = NoteQuery(text=q, tag=tag, mine=mine)
    items = await notes.notes(principal.user_id, query, before=before, limit=limit)
    return [NoteSummaryOut.of(n) for n in items]


@notes_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a note",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def create_note(principal: CurrentPrincipal, notes: Notes, body: NoteCreate) -> NoteOut:
    return NoteOut.of(await notes.create_note(principal.user_id, **body.model_dump()))


@notes_router.get("/{note_id}", summary="One note", responses={401: _PROBLEM, 404: _PROBLEM})
async def get_note(principal: CurrentPrincipal, notes: Notes, note_id: uuid.UUID) -> NoteOut:
    return NoteOut.of(await notes.get_note(principal.user_id, note_id))


@notes_router.patch(
    "/{note_id}",
    summary="Change a note (owner only)",
    responses={401: _PROBLEM, 403: _PROBLEM, 404: _PROBLEM, 422: _PROBLEM},
)
async def update_note(
    principal: CurrentPrincipal, notes: Notes, note_id: uuid.UUID, body: NotePatch
) -> NoteOut:
    changes = body.model_dump(exclude_unset=True)
    return NoteOut.of(await notes.update_note(principal.user_id, note_id, changes))


@notes_router.delete(
    "/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a note (owner only)",
    responses={401: _PROBLEM, 403: _PROBLEM, 404: _PROBLEM},
)
async def delete_note(principal: CurrentPrincipal, notes: Notes, note_id: uuid.UUID) -> Response:
    await notes.delete_note(principal.user_id, note_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------- memories


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    statement: str
    source: MemorySource = Field(description="Where it was learned: a chat, a note or the user")
    source_id: uuid.UUID | None = Field(
        description="The chat thread or note; it may have been deleted since"
    )
    confidence: float = Field(ge=0, le=1)
    created_at: datetime
    updated_at: datetime
    last_confirmed_at: datetime

    @classmethod
    def of(cls, memory: Memory) -> MemoryOut:
        return cls.model_validate(memory)


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=STATEMENT_LENGTH)


@memories_router.get(
    "",
    summary="The caller's memories, newest first",
    description="Page with `before`: pass the id of the last memory you have.",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def list_memories(
    principal: CurrentPrincipal,
    memory: Memories,
    q: Annotated[str | None, Query(max_length=QUERY_LENGTH, description=_WORDS)] = None,
    before: Annotated[uuid.UUID | None, Query(description="Only older than this memory")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> list[MemoryOut]:
    items = await memory.memories(principal.user_id, text=q, before=before, limit=limit)
    return [MemoryOut.of(m) for m in items]


@memories_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Tell the assistant a fact to remember",
    description="A statement the caller already has is confirmed rather than added again.",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def create_memory(principal: CurrentPrincipal, memory: Memories, body: MemoryIn) -> MemoryOut:
    stored = await memory.remember(principal.user_id, body.statement, source=MemorySource.USER)
    return MemoryOut.of(stored)


@memories_router.patch(
    "/{memory_id}",
    summary="Correct a memory",
    responses={401: _PROBLEM, 404: _PROBLEM, 422: _PROBLEM},
)
async def update_memory(
    principal: CurrentPrincipal, memory: Memories, memory_id: uuid.UUID, body: MemoryIn
) -> MemoryOut:
    return MemoryOut.of(await memory.update_memory(principal.user_id, memory_id, body.statement))


@memories_router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Forget a memory",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def delete_memory(
    principal: CurrentPrincipal, memory: Memories, memory_id: uuid.UUID
) -> Response:
    await memory.forget(principal.user_id, memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
