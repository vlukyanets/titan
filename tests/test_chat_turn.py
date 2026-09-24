"""The chat_turn graph and the runtime that runs turns, against a fake `query`."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_agent_node import API_KEY, FakeQuery, init, result
from titan.agent.chat import (
    EventMapper,
    TextDelta,
    ToolActivity,
    render_prompt,
)
from titan.agent.runtime import ChatRuntime, TurnEnded, conninfo, disable_tracing
from titan.domains.accounts.models import Role
from titan.domains.accounts.service import AccountsService, Principal
from titan.domains.chat.models import ChatMessage, MessageRole, MessageStatus
from titan.domains.chat.service import ChatService, TurnUsage
from titan.domains.usage.budget import BudgetService
from titan.settings import Settings

URL = "postgresql+psycopg://user:pw@db/titan"
SECRET = "my passport number is 1234-FAKE"


def delta(text: str, parent: str | None = None) -> StreamEvent:
    return StreamEvent(
        uuid=str(uuid.uuid4()),
        session_id="s1",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        parent_tool_use_id=parent,
    )


def message(role: MessageRole, content: str) -> ChatMessage:
    return ChatMessage(role=role, status=MessageStatus.COMPLETE, content=content)


def test_prompt_puts_history_before_the_new_message() -> None:
    now = datetime(2026, 9, 24, 7, 30, tzinfo=UTC)
    prompt = render_prompt(
        [message(MessageRole.USER, "Hi"), message(MessageRole.ASSISTANT, "Hello!")],
        "What is due?",
        now,
    )
    assert prompt == (
        "<conversation>\n"
        '<message role="user">\nHi\n</message>\n'
        '<message role="assistant">\nHello!\n</message>\n'
        "</conversation>\n"
        "\n"
        "Current time: 2026-09-24T07:30+00:00\n"
        "\n"
        "What is due?"
    )
    assert render_prompt([], "Hi", now) == "Current time: 2026-09-24T07:30+00:00\n\nHi"


def test_events_show_the_main_agent_only() -> None:
    mapper = EventMapper()
    tool = ToolUseBlock(id="t1", name="mcp__tasks__list", input={})
    failing = ToolUseBlock(id="t2", name="mcp__tasks__update", input={})
    messages: list[Message] = [
        delta("Let me "),
        delta("hidden", parent="t0"),
        AssistantMessage(content=[TextBlock(text="Let me check"), tool, failing], model="m"),
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id="t1", content="[]"),
                ToolResultBlock(tool_use_id="t2", content="denied", is_error=True),
                ToolResultBlock(tool_use_id="unknown", content="x"),
            ]
        ),
        UserMessage(content="plain text"),
    ]
    assert [e for m in messages for e in mapper.events(m)] == [
        TextDelta("Let me "),
        ToolActivity("t1", "mcp__tasks__list", "started"),
        ToolActivity("t2", "mcp__tasks__update", "started"),
        ToolActivity("t1", "mcp__tasks__list", "finished"),
        ToolActivity("t2", "mcp__tasks__update", "failed"),
    ]


def test_tracing_variables_are_removed() -> None:
    environ = {"LANGSMITH_TRACING": "true", "LANGCHAIN_API_KEY": "fake", "PATH": "/usr/bin"}
    disable_tracing(environ)
    assert environ == {"PATH": "/usr/bin"}


def test_conninfo_keeps_the_password_for_libpq() -> None:
    assert conninfo(URL) == "postgresql://user:pw@db/titan"


def polluted() -> dict[str, str]:
    return {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": API_KEY,
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fake",
        "LANGSMITH_TRACING": "true",
    }


async def test_ready_cleans_the_environment_once(tmp_path: Path) -> None:
    settings = Settings(database_url=URL, claude_config_dir=tmp_path / "claude")
    environ = polluted()
    fake = FakeQuery(init())
    runtime = ChatRuntime(settings, None, query_fn=fake, environ=environ)  # type: ignore[arg-type]
    assert await runtime.ready()
    assert await runtime.ready()
    assert len(fake.calls) == 1
    assert environ == {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": API_KEY,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }


async def test_a_missing_credential_leaves_chat_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(database_url=URL, claude_auth_mode="oauth", claude_config_dir=tmp_path)
    fake = FakeQuery(init("none"))
    runtime = ChatRuntime(
        settings,
        None,  # type: ignore[arg-type]
        query_fn=fake,
        environ={"ANTHROPIC_API_KEY": API_KEY},
    )
    with caplog.at_level(logging.ERROR):
        assert not await runtime.ready()
    assert fake.calls == []
    assert "CLAUDE_CODE_OAUTH_TOKEN is not set" in caplog.text
    assert API_KEY not in caplog.text


# ------------------------------------------------------------------ with a database


class SlowQuery(FakeQuery):
    """Yields init, then waits until released, then the rest."""

    def __init__(self, *messages: Message) -> None:
        super().__init__(*messages)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def _run(self) -> AsyncIterator[Message]:
        try:
            yield self.messages[0]
            self.started.set()
            await self.release.wait()
            for item in self.messages[1:]:
                yield item
        finally:
            self.closed = True


class Harness:
    def __init__(self, db_url: str, tmp_path: Path, **settings: Any) -> None:
        self.settings = Settings(
            database_url=db_url,
            claude_config_dir=tmp_path / "claude",
            claude_model_strong="strong-model",
            claude_model_fast="fast-model",
            **settings,
        )
        self.engine = create_async_engine(db_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.environ = {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": API_KEY,
            "CLAUDE_CONFIG_DIR": str(self.settings.claude_config_dir),
        }

    def runtime(self, fake: FakeQuery) -> ChatRuntime:
        return ChatRuntime(self.settings, self.sessions, query_fn=fake, environ=self.environ)

    async def principal(self) -> Principal:
        async with self.sessions() as session:
            user = await AccountsService(session).create_user(
                "anna", "a-long-test-password", role=Role.MEMBER
            )
            return Principal(user.id, user.username, user.role, uuid.uuid4())

    async def turn(self, actor: Principal, content: str, thread_id: uuid.UUID | None = None) -> Any:
        async with self.sessions() as session:
            chat = ChatService(session)
            if thread_id is None:
                thread_id = (await chat.create_thread(actor)).id
            return await chat.start_turn(actor, thread_id, content)

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()


@pytest.fixture
async def harness(db_url: str, tmp_path: Path) -> AsyncIterator[Harness]:
    h = Harness(db_url, tmp_path)
    yield h
    await h.engine.dispose()


async def collect(stream: Any) -> list[Any]:
    return [event async for event in stream]


async def test_a_turn_streams_and_stores_the_reply(harness: Harness) -> None:
    anna = await harness.principal()
    first = await harness.turn(anna, "Hi, I am Anna")
    async with harness.sessions() as session:
        await ChatService(session).finish_turn(
            first.assistant_message.id, "Hello Anna!", TurnUsage(model="m")
        )
    turn = await harness.turn(anna, "What is due today?", first.thread.id)
    fake = FakeQuery(
        init(),
        delta("Two tasks "),
        delta("are due."),
        AssistantMessage(content=[TextBlock(text="Two tasks are due.")], model="strong-model"),
        result(result="Two tasks are due."),
    )
    runtime = harness.runtime(fake)
    try:
        events = await collect(runtime.start(anna.user_id, turn.assistant_message.id))
    finally:
        await runtime.aclose()

    assert events[:2] == [TextDelta("Two tasks "), TextDelta("are due.")]
    assert isinstance(events[2], TurnEnded)
    stored = events[2].message
    assert stored is not None
    assert stored.status is MessageStatus.COMPLETE
    assert stored.content == "Two tasks are due."
    assert (stored.model, stored.input_tokens, stored.output_tokens) == ("strong-model", 120, 30)
    assert (stored.cache_read_tokens, stored.cache_creation_tokens) == (900, 50)
    assert stored.cost_usd == pytest.approx(0.0123)

    prompt, options = fake.calls[0]
    assert "Hi, I am Anna" in prompt
    assert "Hello Anna!" in prompt
    assert prompt.endswith("What is due today?")
    assert options.model == "strong-model"
    assert options.include_partial_messages is True
    assert options.tools == []
    assert await harness.scalar("SELECT count(*) FROM checkpoints") == 0
    usage = await harness.scalar(
        "SELECT row(source, model, input_tokens, output_tokens, cost_usd)::text "
        "FROM usage_records WHERE reference = :ref",
        ref=turn.assistant_message.id,
    )
    assert usage == "(chat,strong-model,120,30,0.0123)"


async def test_checkpoints_hold_ids_but_no_content(harness: Harness) -> None:
    anna = await harness.principal()
    turn = await harness.turn(anna, SECRET)
    seen: dict[str, Any] = {}

    class Inspecting(FakeQuery):
        def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
            seen["prompt"] = prompt
            return super().__call__(prompt=prompt, options=options)

        async def _run(self) -> AsyncIterator[Message]:
            # The input checkpoint is written before the reply node runs.
            seen["checkpoints"] = await harness.scalar("SELECT count(*) FROM checkpoints")
            seen["dump"] = await harness.scalar(
                "SELECT string_agg(c.checkpoint::text || c.metadata::text, '') || "
                "coalesce(string_agg(encode(b.blob, 'escape'), ''), '') "
                "FROM checkpoints c LEFT JOIN checkpoint_blobs b USING (thread_id)"
            )
            async for item in super()._run():
                yield item

    runtime = harness.runtime(Inspecting(init(), result()))
    try:
        await collect(runtime.start(anna.user_id, turn.assistant_message.id))
    finally:
        await runtime.aclose()
    assert SECRET in seen["prompt"]
    assert seen["checkpoints"] >= 1
    assert str(turn.assistant_message.id) in seen["dump"]
    assert "passport" not in seen["dump"]


async def test_a_failed_session_fails_the_turn_without_content(harness: Harness) -> None:
    anna = await harness.principal()
    turn = await harness.turn(anna, SECRET)
    fake = FakeQuery(init(), result(is_error=True, result=SECRET, api_error_status=529))
    runtime = harness.runtime(fake)
    try:
        events = await collect(runtime.start(anna.user_id, turn.assistant_message.id))
    finally:
        await runtime.aclose()
    (ended,) = events
    assert ended.message.status is MessageStatus.FAILED
    assert ended.message.error == "the session failed: success, HTTP 529"
    assert ended.message.content == ""
    assert await harness.scalar("SELECT count(*) FROM checkpoints") == 0
    # The failed session still used tokens, and they count.
    assert await harness.scalar("SELECT sum(input_tokens) FROM usage_records") == 120


async def test_over_budget_chat_falls_back_to_the_fast_tier(harness: Harness) -> None:
    anna = await harness.principal()
    async with harness.sessions() as session:
        await BudgetService(session).set_limit(None, anna.user_id, Decimal("0.02"))
    fake = FakeQuery(init(), result(result="Done."))  # every session costs 0.0123
    runtime = harness.runtime(fake)

    async def turn() -> None:
        started = await harness.turn(anna, "Plan my day")
        (ended,) = await collect(runtime.start(anna.user_id, started.assistant_message.id))
        assert ended.message.status is MessageStatus.COMPLETE

    try:
        await turn()
        await turn()  # 0.0246 of 0.02: exceeded from now on
        await turn()
        async with harness.sessions() as session:
            await BudgetService(session).set_limit(None, anna.user_id, Decimal("1"))
        await turn()
    finally:
        await runtime.aclose()

    models = [options.model for _, options in fake.calls]
    assert models == ["strong-model", "strong-model", "fast-model", "strong-model"]
    states = await harness.scalar(
        "SELECT string_agg(data->>'state', ',' ORDER BY created_at) FROM notifications "
        "WHERE kind = 'budget' AND user_id = :user",
        user=anna.user_id,
    )
    assert states == "exceeded"


async def test_a_slow_turn_times_out(db_url: str, tmp_path: Path) -> None:
    h = Harness(db_url, tmp_path, chat_turn_timeout_seconds=0.5)
    try:
        anna = await h.principal()
        turn = await h.turn(anna, "hello")
        runtime = h.runtime(SlowQuery(init(), result()))
        try:
            events = await collect(runtime.start(anna.user_id, turn.assistant_message.id))
        finally:
            await runtime.aclose()
        assert events[-1].message.status is MessageStatus.FAILED
        assert events[-1].message.error == "the reply took longer than 0.5 seconds"
    finally:
        await h.engine.dispose()


async def test_a_turn_outlives_its_listener(harness: Harness) -> None:
    anna = await harness.principal()
    turn = await harness.turn(anna, "hello")
    fake = SlowQuery(init(), delta("Hi"), result(result="Hi there"))
    runtime = harness.runtime(fake)
    try:
        stream = runtime.start(anna.user_id, turn.assistant_message.id)
        await fake.started.wait()
        stream.detach()
        fake.release.set()
        await runtime.wait()
    finally:
        await runtime.aclose()
    async with harness.sessions() as session:
        stored = await ChatService(session).get_message(turn.assistant_message.id)
    assert stored is not None
    assert stored.status is MessageStatus.COMPLETE
    assert stored.content == "Hi there"


async def test_shutdown_fails_running_turns(harness: Harness) -> None:
    anna = await harness.principal()
    turn = await harness.turn(anna, "hello")
    fake = SlowQuery(init(), result())
    runtime = harness.runtime(fake)
    stream = runtime.start(anna.user_id, turn.assistant_message.id)
    await fake.started.wait()
    await runtime.aclose()
    events = await collect(stream)
    assert events[-1].message.status is MessageStatus.FAILED
    assert events[-1].message.error == "the server stopped during the reply"
    assert fake.closed
