"""Notes, who they are shared with, and the memories the agent keeps per user."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base, str_enum
from titan.storage.ids import uuid7


class MemorySource(enum.StrEnum):
    CHAT = "chat"
    NOTE = "note"
    USER = "user"


class Note(Base):
    __tablename__ = "notes"
    # Lists are per owner, most recently changed first.
    __table_args__ = (Index("ix_notes_owner_id_updated_at", "owner_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NoteShare(Base):
    """A user a note is shared with, besides its owner."""

    __tablename__ = "note_shares"
    # Looked up from the reader's side when listing what they can see.
    __table_args__ = (Index("ix_note_shares_user_id", "user_id"),)

    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_owner_id_id", "owner_id", "id"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
        CheckConstraint("(source = 'user') = (source_id IS NULL)", name="source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    statement: Mapped[str] = mapped_column(String(500))
    source: Mapped[MemorySource] = mapped_column(str_enum(MemorySource, "memory_source"))
    # A chat thread or note id. Deliberately not a foreign key: the memory
    # outlives what it was learned from.
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    confidence: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
