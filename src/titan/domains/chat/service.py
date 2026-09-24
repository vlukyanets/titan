"""Chat threads, messages and the bookkeeping of turns.

The service stores what a turn needs and what it produced; running the agent is
the agent runtime's job. Workflow state refers to messages by id only, and the
content is read from here when a turn runs (ADR 0009).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import CursorResult, delete, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.accounts.service import Principal
from titan.domains.chat.errors import InvalidMessageError, NotFoundError, TurnInProgressError
from titan.domains.chat.models import ChatMessage, ChatThread, MessageRole, MessageStatus

DEFAULT_PAGE = 50
MAX_PAGE = 100
MAX_MESSAGE_LENGTH = 8000
TITLE_LENGTH = 80
ERROR_LENGTH = 200
DEFAULT_TURN_TIMEOUT = timedelta(minutes=5)
STALE_TURN_ERROR = "the turn did not finish"


def _now() -> datetime:
    return datetime.now(UTC)


def title_from(content: str) -> str:
    """A thread title from its first message, so naming costs no model call."""
    text = " ".join(content.split())
    if len(text) <= TITLE_LENGTH:
        return text
    return text[: TITLE_LENGTH - 1].rstrip() + "…"


@dataclass(frozen=True)
class Turn:
    thread: ChatThread
    user_message: ChatMessage
    assistant_message: ChatMessage


@dataclass(frozen=True)
class TurnContext:
    """What the agent needs to write a reply."""

    thread_id: uuid.UUID
    user_message: ChatMessage
    # Earlier complete messages of the thread, oldest first.
    history: list[ChatMessage]


@dataclass(frozen=True)
class TurnUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float | None = None


class ChatService:
    def __init__(
        self, session: AsyncSession, *, turn_timeout: timedelta = DEFAULT_TURN_TIMEOUT
    ) -> None:
        self.session = session
        # A turn still streaming after twice its time limit was lost with its node.
        self.stale_after = 2 * turn_timeout

    # ------------------------------------------------------------- threads

    async def create_thread(self, actor: Principal, title: str = "") -> ChatThread:
        thread = ChatThread(user_id=actor.user_id, title=title_from(title))
        self.session.add(thread)
        await self.session.commit()
        return thread

    async def threads(
        self, actor: Principal, *, before: uuid.UUID | None = None, limit: int = DEFAULT_PAGE
    ) -> list[ChatThread]:
        query = select(ChatThread).where(ChatThread.user_id == actor.user_id)
        if before is not None:
            cursor = await self.get_thread(actor, before)
            query = query.where(
                tuple_(ChatThread.updated_at, ChatThread.id) < (cursor.updated_at, cursor.id)
            )
        query = query.order_by(ChatThread.updated_at.desc(), ChatThread.id.desc()).limit(
            max(1, min(limit, MAX_PAGE))
        )
        return list((await self.session.scalars(query)).all())

    async def get_thread(self, actor: Principal, thread_id: uuid.UUID) -> ChatThread:
        thread = await self.session.get(ChatThread, thread_id)
        # Someone else's thread is reported as missing, so ids cannot be probed.
        if thread is None or thread.user_id != actor.user_id:
            raise NotFoundError("thread not found")
        return thread

    async def delete_thread(self, actor: Principal, thread_id: uuid.UUID) -> None:
        thread = await self.get_thread(actor, thread_id)
        await self.session.execute(delete(ChatMessage).where(ChatMessage.thread_id == thread.id))
        await self.session.delete(thread)
        await self.session.commit()

    # ------------------------------------------------------------ messages

    async def messages(
        self,
        actor: Principal,
        thread_id: uuid.UUID,
        *,
        before: uuid.UUID | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[ChatMessage]:
        thread = await self.get_thread(actor, thread_id)
        if await self._expire_stale(thread.id):
            await self.session.commit()
        query = select(ChatMessage).where(ChatMessage.thread_id == thread.id)
        if before is not None:
            query = query.where(ChatMessage.id < before)
        query = query.order_by(ChatMessage.id.desc()).limit(max(1, min(limit, MAX_PAGE)))
        return list((await self.session.scalars(query)).all())

    # --------------------------------------------------------------- turns

    async def start_turn(self, actor: Principal, thread_id: uuid.UUID, content: str) -> Turn:
        """Store the user's message and an empty assistant message that is streaming."""
        content = content.strip()
        if not content:
            raise InvalidMessageError("the message is empty")
        if len(content) > MAX_MESSAGE_LENGTH:
            raise InvalidMessageError(f"the message is longer than {MAX_MESSAGE_LENGTH} characters")
        # Lock the thread row so two requests on this node cannot both start a turn.
        # Two nodes at the same moment can; one user typing on two phones at once
        # is rare enough, and both turns still finish.
        thread = await self.session.scalar(
            select(ChatThread)
            .where(ChatThread.id == thread_id, ChatThread.user_id == actor.user_id)
            .with_for_update()
        )
        if thread is None:
            raise NotFoundError("thread not found")
        await self._expire_stale(thread.id)
        running = await self.session.scalar(
            select(ChatMessage.id).where(
                ChatMessage.thread_id == thread.id,
                ChatMessage.status == MessageStatus.STREAMING,
            )
        )
        if running is not None:
            # Commit, not roll back: it releases the lock, keeps any stale turn
            # just marked failed, and does not expire the caller's objects.
            await self.session.commit()
            raise TurnInProgressError("a reply is still being written in this thread")
        user_message = ChatMessage(
            thread_id=thread.id,
            role=MessageRole.USER,
            status=MessageStatus.COMPLETE,
            content=content,
        )
        self.session.add(user_message)
        await self.session.flush()
        assistant_message = ChatMessage(
            thread_id=thread.id, role=MessageRole.ASSISTANT, status=MessageStatus.STREAMING
        )
        self.session.add(assistant_message)
        if not thread.title:
            thread.title = title_from(content)
        thread.updated_at = _now()
        await self.session.commit()
        return Turn(thread, user_message, assistant_message)

    async def turn_context(
        self, user_id: uuid.UUID, assistant_message_id: uuid.UUID, *, history_limit: int
    ) -> TurnContext:
        """The user message a streaming assistant message answers, and what came before."""
        row = (
            await self.session.execute(
                select(ChatMessage, ChatThread)
                .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
                .where(ChatMessage.id == assistant_message_id, ChatThread.user_id == user_id)
            )
        ).first()
        if row is None or row[0].status is not MessageStatus.STREAMING:
            raise NotFoundError("no running turn with this id")
        assistant, thread = row
        user_message = await self.session.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.thread_id == thread.id,
                ChatMessage.role == MessageRole.USER,
                ChatMessage.id < assistant.id,
            )
            .order_by(ChatMessage.id.desc())
            .limit(1)
        )
        if user_message is None:
            raise NotFoundError("no running turn with this id")
        earlier = (
            await self.session.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.thread_id == thread.id,
                    ChatMessage.status == MessageStatus.COMPLETE,
                    ChatMessage.id < user_message.id,
                )
                .order_by(ChatMessage.id.desc())
                .limit(max(0, history_limit))
            )
        ).all()
        return TurnContext(thread.id, user_message, list(reversed(earlier)))

    async def finish_turn(
        self, assistant_message_id: uuid.UUID, content: str, usage: TurnUsage
    ) -> ChatMessage | None:
        """Store the reply. Returns None if the turn was already failed or deleted."""
        return await self._end(
            assistant_message_id,
            status=MessageStatus.COMPLETE,
            content=content,
            model=usage.model[:64],
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_usd=usage.cost_usd,
        )

    async def fail_turn(self, assistant_message_id: uuid.UUID, error: str) -> ChatMessage | None:
        """Mark the turn failed. The error must not contain message content."""
        return await self._end(
            assistant_message_id, status=MessageStatus.FAILED, error=error[:ERROR_LENGTH]
        )

    async def get_message(self, message_id: uuid.UUID) -> ChatMessage | None:
        return await self.session.get(ChatMessage, message_id, populate_existing=True)

    async def _end(self, message_id: uuid.UUID, **values: Any) -> ChatMessage | None:
        now = _now()
        message = await self.session.scalar(
            update(ChatMessage)
            .where(ChatMessage.id == message_id, ChatMessage.status == MessageStatus.STREAMING)
            .values(completed_at=now, **values)
            .returning(ChatMessage)
            .execution_options(populate_existing=True)
        )
        if message is not None:
            await self.session.execute(
                update(ChatThread).where(ChatThread.id == message.thread_id).values(updated_at=now)
            )
        await self.session.commit()
        return message

    async def _expire_stale(self, thread_id: uuid.UUID) -> bool:
        result: CursorResult[Any] = await self.session.execute(  # type: ignore[assignment]
            update(ChatMessage)
            .where(
                ChatMessage.thread_id == thread_id,
                ChatMessage.status == MessageStatus.STREAMING,
                ChatMessage.created_at < _now() - self.stale_after,
            )
            .values(status=MessageStatus.FAILED, error=STALE_TURN_ERROR, completed_at=_now())
        )
        return bool(result.rowcount)
