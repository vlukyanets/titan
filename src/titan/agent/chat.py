"""The `chat_turn` workflow: one user message in, one streamed reply out.

The graph state holds only ids (ADR 0009). The `reply` node reads the thread
from the chat domain, runs one Agent SDK session and sends what the model
writes to the LangGraph `custom` stream as `TextDelta` and `ToolActivity`
events. Running turns, time limits and failures are handled by
`titan.agent.runtime`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, TypedDict

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from titan.agent.node import QueryFn, Tier, agent_options, agent_run, stream_agent
from titan.domains.chat.models import ChatMessage, MessageRole
from titan.domains.chat.service import ChatService, TurnUsage
from titan.settings import Settings

# Kept free of per-turn values, so Claude's prompt cache can reuse it.
SYSTEM_PROMPT = """\
You are TITAN, a self-hosted personal assistant, planner and tracker. You help \
one member of a household plan their days, keep track of tasks, habits and \
notes, and think things through.

- Reply in the language of the user's latest message.
- Be concise and concrete. Use short paragraphs or lists when they help.
- Earlier messages of this conversation are given in <conversation>. When you \
refer back to something, name it, because the next turn sees only the text of \
your replies.
- You have no tools yet. If the user asks you to change their data, say that \
this is not possible yet instead of pretending it was done.
"""
MAX_TURNS = 10


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolActivity:
    id: str
    name: str
    status: Literal["started", "finished", "failed"]


TurnEvent = TextDelta | ToolActivity


class ChatTurnState(TypedDict):
    user_id: str
    assistant_message_id: str


@dataclass(frozen=True)
class ChatContext:
    """Per-run dependencies. Passed as LangGraph context, never checkpointed."""

    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    query_fn: QueryFn = query
    environ: Mapping[str, str] | None = None


def render_prompt(history: Sequence[ChatMessage], message: str, now: datetime) -> str:
    """The session prompt: earlier messages, the current time, then the new message."""
    lines: list[str] = []
    if history:
        lines.append("<conversation>")
        for item in history:
            role = "user" if item.role is MessageRole.USER else "assistant"
            lines.append(f'<message role="{role}">\n{item.content}\n</message>')
        lines.append("</conversation>")
        lines.append("")
    lines.append(f"Current time: {now.astimezone(UTC).isoformat(timespec='minutes')}")
    lines.append("")
    lines.append(message)
    return "\n".join(lines)


@dataclass
class EventMapper:
    """Turns SDK messages into turn events. Only the main agent's output is shown."""

    tool_names: dict[str, str] = field(default_factory=dict)

    def events(self, message: Message) -> list[TurnEvent]:
        if isinstance(message, StreamEvent):
            event = message.event
            delta = event.get("delta") or {}
            if (
                message.parent_tool_use_id is None
                and event.get("type") == "content_block_delta"
                and delta.get("type") == "text_delta"
                and delta.get("text")
            ):
                return [TextDelta(str(delta["text"]))]
            return []
        if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
            started: list[TurnEvent] = []
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    self.tool_names[block.id] = block.name
                    started.append(ToolActivity(block.id, block.name, "started"))
            return started
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            ended: list[TurnEvent] = []
            for block in message.content:
                if isinstance(block, ToolResultBlock) and block.tool_use_id in self.tool_names:
                    status: Literal["finished", "failed"] = (
                        "failed" if block.is_error else "finished"
                    )
                    name = self.tool_names.pop(block.tool_use_id)
                    ended.append(ToolActivity(block.tool_use_id, name, status))
            return ended
        return []


async def reply(state: ChatTurnState, runtime: Runtime[ChatContext]) -> dict[str, str]:
    context = runtime.context
    settings = context.settings
    message_id = uuid.UUID(state["assistant_message_id"])
    async with context.sessions() as session:
        turn = await ChatService(session).turn_context(
            uuid.UUID(state["user_id"]), message_id, history_limit=settings.chat_history_messages
        )
    options = agent_options(
        settings,
        tier=Tier.STRONG,
        system_prompt=SYSTEM_PROMPT,
        max_turns=MAX_TURNS,
        partial_messages=True,
    )
    prompt = render_prompt(turn.history, turn.user_message.content, datetime.now(UTC))
    mapper = EventMapper()
    result: ResultMessage | None = None
    async for message in stream_agent(
        prompt, options, settings, query_fn=context.query_fn, environ=context.environ
    ):
        for event in mapper.events(message):
            runtime.stream_writer(event)
        if isinstance(message, ResultMessage):
            result = message
    run = agent_run(result, options)
    async with context.sessions() as session:
        await ChatService(session).finish_turn(
            message_id,
            run.text,
            TurnUsage(
                model=run.model,
                input_tokens=run.usage.input_tokens,
                output_tokens=run.usage.output_tokens,
                cache_read_tokens=run.usage.cache_read_tokens,
                cache_creation_tokens=run.usage.cache_creation_tokens,
                cost_usd=run.cost_usd,
            ),
        )
    return {}


def build_graph(
    checkpointer: BaseCheckpointSaver[str] | None,
) -> CompiledStateGraph[ChatTurnState, ChatContext, ChatTurnState, ChatTurnState]:
    graph = StateGraph(ChatTurnState, context_schema=ChatContext)
    graph.add_node("reply", reply)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    return graph.compile(checkpointer=checkpointer, name="chat_turn")


def thread_key(assistant_message_id: uuid.UUID) -> str:
    """The checkpointer thread of one turn."""
    return f"chat-turn:{assistant_message_id}"
