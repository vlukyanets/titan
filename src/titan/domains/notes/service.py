"""Notes and memories, with the rules of docs/spec/domains/notes-memory.md.

Every method takes the acting user's id and checks access itself. Items the
actor cannot see are reported as missing, so ids cannot be probed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, Text, and_, delete, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.sql.elements import SQLCoreOperations

from titan.domains.accounts.models import User
from titan.domains.chat.models import ChatThread
from titan.domains.notes.errors import (
    ForbiddenError,
    InvalidMemoryError,
    InvalidNoteError,
    NotFoundError,
)
from titan.domains.notes.models import Memory, MemorySource, Note, NoteShare
from titan.storage.collation import UNICODE

DEFAULT_PAGE = 50
MAX_PAGE = 100
TITLE_LENGTH = 200
BODY_LENGTH = 100_000
TAG_LENGTH = 32
MAX_TAGS = 20
MAX_SHARED = 50
EXCERPT_LENGTH = 200
STATEMENT_LENGTH = 500
QUERY_LENGTH = 200
MAX_WORDS = 10

NOTE_FIELDS = frozenset({"title", "body", "tags", "shared_with"})


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: object, name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise InvalidNoteError(f"{name} is not an id") from None


def _title(value: object) -> str:
    title = "" if value is None else " ".join(str(value).split())
    if len(title) > TITLE_LENGTH:
        raise InvalidNoteError(f"the title is longer than {TITLE_LENGTH} characters")
    return title


def _body(value: object) -> str:
    body = "" if value is None else str(value).strip()
    if len(body) > BODY_LENGTH:
        raise InvalidNoteError(f"the body is longer than {BODY_LENGTH} characters")
    return body


def _tags(values: Iterable[object]) -> list[str]:
    tags: list[str] = []
    for value in values:
        tag = str(value).strip().lower()
        if not tag or len(tag) > TAG_LENGTH:
            raise InvalidNoteError(f"a tag must be 1 to {TAG_LENGTH} characters")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise InvalidNoteError(f"a note has at most {MAX_TAGS} tags")
    return tags


def _statement(value: object) -> str:
    statement = "" if value is None else " ".join(str(value).split())
    if not statement:
        raise InvalidMemoryError("the statement is empty")
    if len(statement) > STATEMENT_LENGTH:
        raise InvalidMemoryError(f"the statement is longer than {STATEMENT_LENGTH} characters")
    return statement


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidMemoryError("confidence is a number from 0 to 1")
    confidence = float(value)
    if not 0 <= confidence <= 1:
        raise InvalidMemoryError("confidence is a number from 0 to 1")
    return confidence


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def words(text: str | None) -> list[str]:
    """The words of a search query that count, at most MAX_WORDS."""
    if not text:
        return []
    return text[:QUERY_LENGTH].split()[:MAX_WORDS]


def contains_words(
    columns: Sequence[SQLCoreOperations[str]], text: str | None
) -> ColumnElement[bool]:
    """Every word appears in one of the columns, ignoring case in every alphabet."""
    conditions = []
    for word in words(text):
        pattern = "%" + _escape_like(word) + "%"
        conditions.append(
            or_(*(column.collate(UNICODE).ilike(pattern, escape="\\") for column in columns))
        )
    return and_(True, *conditions)


def _excerpt(start: str) -> str:
    text = " ".join(start.split())
    if len(text) <= EXCERPT_LENGTH:
        return text
    return text[: EXCERPT_LENGTH - 1].rstrip() + "…"


@dataclass(frozen=True)
class SharedNote:
    note: Note
    # Readers besides the owner, in the order they were stored.
    shared_with: list[uuid.UUID]


@dataclass(frozen=True)
class NoteSummary:
    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    excerpt: str
    tags: list[str]
    shared_with: list[uuid.UUID]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NoteQuery:
    text: str | None = None
    tag: str | None = None
    # True: only the actor's own notes; False: only notes shared with them.
    mine: bool | None = None


def readable_by(actor: uuid.UUID) -> ColumnElement[bool]:
    """Notes the actor owns or that are shared with them."""
    shared = exists().where(NoteShare.note_id == Note.id, NoteShare.user_id == actor)
    return or_(Note.owner_id == actor, shared)


class NotesService:
    def __init__(self, session: AsyncSession, *, commit: bool = True) -> None:
        self.session = session
        # Agent tools pass commit=False: their caller commits the change together
        # with its audit entry.
        self._commit = commit

    async def _done(self) -> None:
        if self._commit:
            await self.session.commit()
        else:
            await self.session.flush()

    async def _readers(self, note_id: uuid.UUID) -> list[uuid.UUID]:
        rows = await self.session.scalars(
            select(NoteShare.user_id).where(NoteShare.note_id == note_id)
        )
        return list(rows.all())

    async def _shared_with(self, owner: uuid.UUID, ids: Iterable[object]) -> list[uuid.UUID]:
        wanted: list[uuid.UUID] = []
        for value in ids:
            user_id = _uuid(value, "shared_with")
            if user_id != owner and user_id not in wanted:
                wanted.append(user_id)
        if len(wanted) > MAX_SHARED:
            raise InvalidNoteError(f"a note is shared with at most {MAX_SHARED} users")
        if wanted:
            found = set(
                (
                    await self.session.scalars(
                        select(User.id).where(User.id.in_(wanted), User.disabled_at.is_(None))
                    )
                ).all()
            )
            if len(found) != len(wanted):
                raise InvalidNoteError("shared_with names a user that does not exist")
        return wanted

    async def _note(self, actor: uuid.UUID, note_id: uuid.UUID, *, lock: bool = False) -> Note:
        query = select(Note).where(Note.id == note_id, readable_by(actor))
        if lock:
            query = query.with_for_update(of=Note)
        note = await self.session.scalar(query)
        if note is None:
            raise NotFoundError("note not found")
        return note

    async def _owned(self, actor: uuid.UUID, note_id: uuid.UUID, action: str) -> Note:
        note = await self._note(actor, note_id, lock=True)
        if note.owner_id != actor:
            raise ForbiddenError(f"only the note's owner can {action} it")
        return note

    async def create_note(
        self,
        actor: uuid.UUID,
        *,
        title: str = "",
        body: str = "",
        tags: Iterable[str] = (),
        shared_with: Iterable[uuid.UUID] = (),
    ) -> SharedNote:
        note = Note(owner_id=actor, title=_title(title), body=_body(body), tags=_tags(tags))
        if not note.title and not note.body:
            raise InvalidNoteError("a note needs a title or a body")
        readers = await self._shared_with(actor, shared_with)
        self.session.add(note)
        await self.session.flush()
        self.session.add_all(NoteShare(note_id=note.id, user_id=r) for r in readers)
        await self._done()
        return SharedNote(note, readers)

    async def notes(
        self,
        actor: uuid.UUID,
        query: NoteQuery | None = None,
        *,
        before: uuid.UUID | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[NoteSummary]:
        """Most recently changed first, with an excerpt instead of the body."""
        query = query or NoteQuery()
        start = func.left(Note.body, EXCERPT_LENGTH * 2)
        stmt = select(Note, start).options(defer(Note.body)).where(readable_by(actor))
        if query.mine is True:
            stmt = stmt.where(Note.owner_id == actor)
        elif query.mine is False:
            stmt = stmt.where(Note.owner_id != actor)
        if query.tag:
            stmt = stmt.where(Note.tags.contains([query.tag.strip().lower()]))
        if query.text:
            tags = func.array_to_string(Note.tags, " ", type_=Text)
            stmt = stmt.where(contains_words([Note.title, Note.body, tags], query.text))
        if before is not None:
            cursor = await self.session.scalar(
                select(Note.updated_at).where(Note.id == before, readable_by(actor))
            )
            if cursor is None:
                raise InvalidNoteError("before names no note you can see")
            stmt = stmt.where(
                or_(Note.updated_at < cursor, and_(Note.updated_at == cursor, Note.id < before))
            )
        stmt = stmt.order_by(Note.updated_at.desc(), Note.id.desc()).limit(
            max(1, min(limit, MAX_PAGE))
        )
        rows = (await self.session.execute(stmt)).all()
        readers: dict[uuid.UUID, list[uuid.UUID]] = {note.id: [] for note, _ in rows}
        if readers:
            shares = await self.session.execute(
                select(NoteShare.note_id, NoteShare.user_id).where(NoteShare.note_id.in_(readers))
            )
            for note_id, user_id in shares:
                readers[note_id].append(user_id)
        return [
            NoteSummary(
                id=note.id,
                owner_id=note.owner_id,
                title=note.title,
                excerpt=_excerpt(text),
                tags=list(note.tags),
                shared_with=readers[note.id],
                created_at=note.created_at,
                updated_at=note.updated_at,
            )
            for note, text in rows
        ]

    async def get_note(self, actor: uuid.UUID, note_id: uuid.UUID) -> SharedNote:
        note = await self._note(actor, note_id)
        return SharedNote(note, await self._readers(note.id))

    async def update_note(
        self, actor: uuid.UUID, note_id: uuid.UUID, changes: Mapping[str, Any]
    ) -> SharedNote:
        unknown = set(changes) - NOTE_FIELDS
        if unknown:
            raise InvalidNoteError(f"a note has no field {sorted(unknown)[0]}")
        note = await self._owned(actor, note_id, "change")
        # Everything is checked before the note changes, so a refused update
        # leaves it untouched.
        values: dict[str, Any] = {}
        if "title" in changes:
            values["title"] = _title(changes["title"])
        if "body" in changes:
            values["body"] = _body(changes["body"])
        if "tags" in changes:
            values["tags"] = _tags(changes["tags"] or ())
        if not values.get("title", note.title) and not values.get("body", note.body):
            raise InvalidNoteError("a note needs a title or a body")
        readers = await self._readers(note.id)
        if "shared_with" in changes:
            readers = await self._shared_with(actor, changes["shared_with"] or ())
            await self.session.execute(delete(NoteShare).where(NoteShare.note_id == note.id))
            self.session.add_all(NoteShare(note_id=note.id, user_id=r) for r in readers)
        for name, value in values.items():
            setattr(note, name, value)
        note.updated_at = _now()
        await self._done()
        return SharedNote(note, readers)

    async def delete_note(self, actor: uuid.UUID, note_id: uuid.UUID) -> None:
        note = await self._owned(actor, note_id, "delete")
        # Memories learned from the note keep their statement; their link to
        # it simply stops resolving.
        await self.session.execute(delete(NoteShare).where(NoteShare.note_id == note.id))
        await self.session.delete(note)
        await self._done()


class MemoryService:
    """A user's memories. Nobody but their owner ever sees or recalls them."""

    def __init__(self, session: AsyncSession, *, commit: bool = True) -> None:
        self.session = session
        self._commit = commit

    async def _done(self) -> None:
        if self._commit:
            await self.session.commit()
        else:
            await self.session.flush()

    async def memories(
        self,
        actor: uuid.UUID,
        *,
        text: str | None = None,
        before: uuid.UUID | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[Memory]:
        """Newest first; `text` keeps those whose statement has every word."""
        stmt = select(Memory).where(Memory.owner_id == actor)
        if text:
            stmt = stmt.where(contains_words([Memory.statement], text))
        if before is not None:
            stmt = stmt.where(Memory.id < before)
        stmt = stmt.order_by(Memory.id.desc()).limit(max(1, min(limit, MAX_PAGE)))
        return list((await self.session.scalars(stmt)).all())

    async def get_memory(
        self, actor: uuid.UUID, memory_id: uuid.UUID, *, lock: bool = False
    ) -> Memory:
        stmt = select(Memory).where(Memory.id == memory_id, Memory.owner_id == actor)
        if lock:
            stmt = stmt.with_for_update()
        memory = await self.session.scalar(stmt)
        if memory is None:
            raise NotFoundError("memory not found")
        return memory

    async def _same(self, actor: uuid.UUID, statement: str) -> Memory | None:
        # casefold() under the builtin collation, so the match does not depend
        # on the database's locale.
        wanted = func.casefold(literal(statement, Text).collate(UNICODE))
        same: Memory | None = await self.session.scalar(
            select(Memory)
            .where(
                Memory.owner_id == actor,
                func.casefold(Memory.statement.collate(UNICODE)) == wanted,
            )
            .order_by(Memory.id)
            .limit(1)
            .with_for_update()
        )
        return same

    async def _check_source(
        self, actor: uuid.UUID, source: MemorySource, source_id: uuid.UUID | None
    ) -> None:
        if source is MemorySource.USER:
            if source_id is not None:
                raise InvalidMemoryError("a memory typed in by the user has no source id")
            return
        if source_id is None:
            raise InvalidMemoryError(f"a memory from a {source.value} needs its id")
        if source is MemorySource.CHAT:
            found = await self.session.scalar(
                select(ChatThread.id).where(ChatThread.id == source_id, ChatThread.user_id == actor)
            )
        else:
            found = await self.session.scalar(
                select(Note.id).where(Note.id == source_id, readable_by(actor))
            )
        if found is None:
            raise InvalidMemoryError(f"the source {source.value} was not found")

    async def remember(
        self,
        actor: uuid.UUID,
        statement: str,
        *,
        source: MemorySource,
        source_id: uuid.UUID | None = None,
        confidence: float = 1.0,
        now: datetime | None = None,
    ) -> Memory:
        """Store a fact, or confirm the one the user already has."""
        text = _statement(statement)
        level = _confidence(confidence)
        source = MemorySource(source)
        await self._check_source(actor, source, source_id)
        now = now or _now()
        memory = await self._same(actor, text)
        if memory is None:
            memory = Memory(
                owner_id=actor,
                statement=text,
                source=source,
                source_id=source_id,
                confidence=level,
                created_at=now,
                updated_at=now,
                last_confirmed_at=now,
            )
            self.session.add(memory)
        else:
            memory.confidence = max(memory.confidence, level)
            memory.last_confirmed_at = now
            memory.updated_at = now
        await self._done()
        return memory

    async def update_memory(
        self,
        actor: uuid.UUID,
        memory_id: uuid.UUID,
        statement: str,
        *,
        now: datetime | None = None,
    ) -> Memory:
        """The user's own words: full confidence, confirmed now."""
        text = _statement(statement)
        memory = await self.get_memory(actor, memory_id, lock=True)
        now = now or _now()
        memory.statement = text
        memory.confidence = 1.0
        memory.last_confirmed_at = now
        memory.updated_at = now
        await self._done()
        return memory

    async def forget(self, actor: uuid.UUID, memory_id: uuid.UUID) -> None:
        memory = await self.get_memory(actor, memory_id, lock=True)
        await self.session.delete(memory)
        await self._done()
