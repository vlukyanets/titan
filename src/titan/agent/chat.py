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
from datetime import UTC, datetime, timedelta
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
from titan.agent.policy import policy_hooks
from titan.agent.tools import ToolScope, mcp_servers
from titan.agent.usage import record_usage
from titan.domains.autonomy.models import Approval
from titan.domains.chat.models import ChatMessage, MessageRole
from titan.domains.chat.service import ChatService, TurnUsage
from titan.domains.usage.budget import BudgetService
from titan.notify import Pusher
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
- Use your tools to look things up and to act. Never claim an action was done \
unless a tool said so.
- Some actions need the user's approval. When a tool answers that the user has \
been asked, tell them briefly what you asked for and do not call it again.
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


@dataclass(frozen=True)
class ApprovalRequested:
    approval: Approval


TurnEvent = TextDelta | ToolActivity | ApprovalRequested


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
    pusher: Pusher | None = None


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
    user_id = uuid.UUID(state["user_id"])
    async with context.sessions() as session:
        turn = await ChatService(session).turn_context(
            user_id, message_id, history_limit=settings.chat_history_messages
        )
        budget = await BudgetService(session).status(user_id)
    scope = ToolScope(
        context.sessions,
        user_id,
        turn.thread_id,
        context.pusher,
        settings.push_allowed_origins,
    )
    options = agent_options(
        settings,
        # Over the monthly budget, chat goes on with the cheap model.
        tier=Tier.FAST if budget.exceeded else Tier.STRONG,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers=mcp_servers(scope),
        # Domain tools stay out of allowed_tools: only the policy hook lets a
        # call through, and without it Claude Code's own permission check refuses.
        hooks=policy_hooks(
            scope,
            approval_ttl=timedelta(hours=settings.approval_ttl_hours),
            on_approval=lambda approval: runtime.stream_writer(ApprovalRequested(approval)),
        ),
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
    # Before agent_run decides whether the turn failed: failed sessions cost too.
    await record_usage(
        context.sessions,
        result,
        user_id=user_id,
        source="chat",
        model=options.model or "",
        reference=message_id,
        pusher=context.pusher,
    )
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
