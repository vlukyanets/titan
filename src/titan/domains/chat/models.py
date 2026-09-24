"""Chat threads and their messages. The content lives only here (ADR 0009)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base, str_enum
from titan.storage.ids import uuid7


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(enum.StrEnum):
    COMPLETE = "complete"
    STREAMING = "streaming"
    FAILED = "failed"


class ChatThread(Base):
    __tablename__ = "chat_threads"
    # The thread list is per user, most recently active first.
    __table_args__ = (Index("ix_chat_threads_user_id_updated_at", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(80), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    # Messages are read per thread in id order; UUIDv7 ids sort by creation time.
    __table_args__ = (Index("ix_chat_messages_thread_id_id", "thread_id", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_threads.id"))
    role: Mapped[MessageRole] = mapped_column(str_enum(MessageRole, "chat_message_role"))
    status: Mapped[MessageStatus] = mapped_column(str_enum(MessageStatus, "chat_message_status"))
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Set on assistant messages when the turn ends.
    model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_creation_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    # Why a turn failed; never contains message content.
    error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
