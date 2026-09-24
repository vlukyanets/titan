"""Chat threads, the one-turn rule, stale turns and the history window."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService, Principal
from titan.domains.chat.errors import InvalidMessageError, NotFoundError, TurnInProgressError
from titan.domains.chat.models import ChatMessage, MessageRole, MessageStatus
from titan.domains.chat.service import (
    MAX_MESSAGE_LENGTH,
    STALE_TURN_ERROR,
    ChatService,
    TurnUsage,
    title_from,
)

pytestmark = pytest.mark.db


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def principal(sessions: async_sessionmaker[AsyncSession], username: str) -> Principal:
    async with sessions() as session:
        user = await AccountsService(session).create_user(
            username, "a-long-test-password", role=Role.MEMBER
        )
        return Principal(user.id, user.username, user.role, uuid.uuid4())


async def reply(
    sessions: async_sessionmaker[AsyncSession], message_id: uuid.UUID, text: str
) -> ChatMessage | None:
    async with sessions() as session:
        return await ChatService(session).finish_turn(
            message_id, text, TurnUsage(model="test-model", input_tokens=10, output_tokens=5)
        )


def test_titles_come_from_the_first_message() -> None:
    assert title_from("  What is\n due   today? ") == "What is due today?"
    long = title_from("word " * 40)
    assert len(long) == 80
    assert long.endswith("…")


async def test_a_turn_stores_both_messages_and_names_the_thread(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna")
    async with sessions() as session:
        chat = ChatService(session)
        thread = await chat.create_thread(anna)
        assert thread.title == ""
        turn = await chat.start_turn(anna, thread.id, "  Plan my Saturday  ")
    assert turn.user_message.content == "Plan my Saturday"
    assert turn.user_message.status is MessageStatus.COMPLETE
    assert turn.assistant_message.role is MessageRole.ASSISTANT
    assert turn.assistant_message.status is MessageStatus.STREAMING
    assert turn.thread.title == "Plan my Saturday"

    done = await reply(sessions, turn.assistant_message.id, "Here is a plan.")
    assert done is not None
    assert done.status is MessageStatus.COMPLETE
    assert done.content == "Here is a plan."
    assert (done.model, done.input_tokens, done.output_tokens) == ("test-model", 10, 5)
    assert done.completed_at is not None
    # A finished turn cannot be finished or failed again.
    assert await reply(sessions, turn.assistant_message.id, "again") is None
    async with sessions() as session:
        assert await ChatService(session).fail_turn(turn.assistant_message.id, "late") is None


async def test_one_turn_at_a_time(sessions: async_sessionmaker[AsyncSession]) -> None:
    anna = await principal(sessions, "anna")
    async with sessions() as session:
        chat = ChatService(session)
        thread = await chat.create_thread(anna)
        turn = await chat.start_turn(anna, thread.id, "first")
        with pytest.raises(TurnInProgressError):
            await chat.start_turn(anna, thread.id, "second")
        await chat.fail_turn(turn.assistant_message.id, "the session failed: error_max_turns")
        await chat.start_turn(anna, thread.id, "second")


async def test_a_lost_turn_stops_blocking_the_thread(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna")
    timeout = timedelta(minutes=5)
    async with sessions() as session:
        chat = ChatService(session, turn_timeout=timeout)
        thread = await chat.create_thread(anna)
        turn = await chat.start_turn(anna, thread.id, "first")
        await session.execute(
            update(ChatMessage)
            .where(ChatMessage.id == turn.assistant_message.id)
            .values(created_at=datetime.now(UTC) - 2 * timeout - timedelta(seconds=1))
        )
        await session.commit()
        listed = await chat.messages(anna, thread.id)
    assert listed[0].status is MessageStatus.FAILED
    assert listed[0].error == STALE_TURN_ERROR
    async with sessions() as session:
        await ChatService(session, turn_timeout=timeout).start_turn(anna, thread.id, "second")


async def test_messages_are_validated(sessions: async_sessionmaker[AsyncSession]) -> None:
    anna = await principal(sessions, "anna")
    async with sessions() as session:
        chat = ChatService(session)
        thread = await chat.create_thread(anna)
        with pytest.raises(InvalidMessageError):
            await chat.start_turn(anna, thread.id, "   ")
        with pytest.raises(InvalidMessageError):
            await chat.start_turn(anna, thread.id, "x" * (MAX_MESSAGE_LENGTH + 1))


async def test_threads_are_private(sessions: async_sessionmaker[AsyncSession]) -> None:
    anna = await principal(sessions, "anna")
    boris = await principal(sessions, "boris")
    async with sessions() as session:
        chat = ChatService(session)
        thread = await chat.create_thread(anna, "Anna's thread")
        turn = await chat.start_turn(anna, thread.id, "hello")
        with pytest.raises(NotFoundError):
            await chat.get_thread(boris, thread.id)
        with pytest.raises(NotFoundError):
            await chat.messages(boris, thread.id)
        with pytest.raises(NotFoundError):
            await chat.start_turn(boris, thread.id, "hi")
        with pytest.raises(NotFoundError):
            await chat.delete_thread(boris, thread.id)
        with pytest.raises(NotFoundError):
            await chat.turn_context(boris.user_id, turn.assistant_message.id, history_limit=10)
        assert await chat.threads(boris) == []


async def test_turn_context_holds_the_latest_complete_messages(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna")
    async with sessions() as session:
        chat = ChatService(session)
        thread = await chat.create_thread(anna)
        for i in range(3):
            turn = await chat.start_turn(anna, thread.id, f"question {i}")
            if i == 1:
                await chat.fail_turn(turn.assistant_message.id, "the session failed")
            else:
                await reply(sessions, turn.assistant_message.id, f"answer {i}")
        turn = await chat.start_turn(anna, thread.id, "question 3")
        context = await chat.turn_context(anna.user_id, turn.assistant_message.id, history_limit=4)
    assert context.thread_id == thread.id
    assert context.user_message.content == "question 3"
    # The failed reply is left out; its question stays.
    assert [m.content for m in context.history] == [
        "answer 0",
        "question 1",
        "question 2",
        "answer 2",
    ]
    async with sessions() as session:
        await ChatService(session).finish_turn(
            turn.assistant_message.id, "answer 3", TurnUsage(model="m")
        )
        with pytest.raises(NotFoundError):
            await ChatService(session).turn_context(
                anna.user_id, turn.assistant_message.id, history_limit=4
            )


async def test_thread_list_follows_activity_and_pages(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna")
    async with sessions() as session:
        chat = ChatService(session)
        first = await chat.create_thread(anna, "first")
        second = await chat.create_thread(anna, "second")
        third = await chat.create_thread(anna, "third")
        await chat.start_turn(anna, first.id, "bump")
        listed = await chat.threads(anna)
        assert [t.title for t in listed] == ["first", "third", "second"]
        page = await chat.threads(anna, before=third.id, limit=5)
        assert [t.id for t in page] == [second.id]


async def test_deleting_a_thread_removes_its_messages(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    anna = await principal(sessions, "anna")
    async with sessions() as session:
        chat = ChatService(session)
        thread = await chat.create_thread(anna)
        turn = await chat.start_turn(anna, thread.id, "hello")
        await chat.delete_thread(anna, thread.id)
        with pytest.raises(NotFoundError):
            await chat.get_thread(anna, thread.id)
        assert await chat.get_message(turn.user_message.id) is None
    # A turn that ends after its thread was deleted stores nothing.
    assert await reply(sessions, turn.assistant_message.id, "late answer") is None
