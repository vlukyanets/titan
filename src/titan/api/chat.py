"""Chat threads and messages; replies stream as Server-Sent Events."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from titan.agent.chat import TextDelta, ToolActivity
from titan.agent.runtime import TurnEnded, TurnStream
from titan.api.deps import Chat, ChatTurns, CurrentPrincipal
from titan.api.problems import PROBLEM_JSON
from titan.domains.chat.models import ChatMessage, ChatThread, MessageRole, MessageStatus
from titan.domains.chat.service import DEFAULT_PAGE, MAX_MESSAGE_LENGTH, MAX_PAGE, Turn

router = APIRouter(prefix="/chat", tags=["chat"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}


class ThreadOut(BaseModel):
    id: uuid.UUID
    title: str = Field(description="The start of the first message, until one is sent: empty")
    created_at: datetime
    updated_at: datetime = Field(description="When the last message was added")

    @classmethod
    def of(cls, thread: ChatThread) -> ThreadOut:
        return cls(
            id=thread.id,
            title=thread.title,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
        )


class ThreadCreate(BaseModel):
    title: str = Field(default="", max_length=200, description="Optional; cut to 80 characters")


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float | None = Field(description="As reported by Claude Code")


class MessageOut(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID
    role: MessageRole
    status: MessageStatus
    content: str = Field(description="Empty while the reply is streaming or when it failed")
    model: str | None = Field(description="The model that wrote an assistant message")
    usage: Usage | None = Field(description="Token usage of a complete assistant message")
    error: str | None = Field(description="Why the reply failed; never contains message text")
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def of(cls, message: ChatMessage) -> MessageOut:
        usage = None
        if message.model is not None:
            usage = Usage(
                input_tokens=message.input_tokens or 0,
                output_tokens=message.output_tokens or 0,
                cache_read_tokens=message.cache_read_tokens or 0,
                cache_creation_tokens=message.cache_creation_tokens or 0,
                cost_usd=message.cost_usd,
            )
        return cls(
            id=message.id,
            thread_id=message.thread_id,
            role=message.role,
            status=message.status,
            content=message.content,
            model=message.model,
            usage=usage,
            error=message.error,
            created_at=message.created_at,
            completed_at=message.completed_at,
        )


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


# Stream events. Each is sent with its `type` as the SSE event name.


class TurnStartedEvent(BaseModel):
    type: Literal["turn"] = "turn"
    user_message: MessageOut
    assistant_message: MessageOut = Field(description="Status `streaming`, no content yet")


class TextEvent(BaseModel):
    type: Literal["text"] = "text"
    delta: str = Field(description="The next piece of the reply")


class ToolEvent(BaseModel):
    type: Literal["tool"] = "tool"
    id: str
    name: str
    status: Literal["started", "finished", "failed"]


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    message: MessageOut = Field(
        description="The complete reply. Show its content in place of the streamed text"
    )


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    message: MessageOut = Field(description="The failed assistant message, with its error")


ChatEvent = Annotated[
    TurnStartedEvent | TextEvent | ToolEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]


@dataclass(frozen=True)
class StartedTurn:
    turn: Turn
    stream: TurnStream


async def start_turn(
    thread_id: uuid.UUID,
    body: MessageIn,
    principal: CurrentPrincipal,
    chat: Chat,
    turns: ChatTurns,
) -> StartedTurn:
    # Runs as a dependency, so every refusal is a plain problem response sent
    # before the event stream starts.
    if not await turns.ready():
        raise HTTPException(
            status_code=503,
            detail="chat is unavailable on this node: its Claude credential is missing or "
            "was rejected",
        )
    turn = await chat.start_turn(principal, thread_id, body.content)
    return StartedTurn(turn, turns.start(principal.user_id, turn.assistant_message.id))


def _sse(event: TurnStartedEvent | TextEvent | ToolEvent | DoneEvent | ErrorEvent) -> Any:
    # Typed Any: the route's annotation documents the data as ChatEvent, while the
    # ServerSentEvent wrapper adds the event name on the wire.
    return ServerSentEvent(event=event.type, data=event)


@router.post(
    "/threads",
    status_code=status.HTTP_201_CREATED,
    summary="Start a thread",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def create_thread(
    principal: CurrentPrincipal, chat: Chat, body: ThreadCreate | None = None
) -> ThreadOut:
    thread = await chat.create_thread(principal, body.title if body else "")
    return ThreadOut.of(thread)


@router.get(
    "/threads",
    summary="The caller's threads, most recently active first",
    description="Page with `before`: pass the id of the last thread you have.",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def list_threads(
    principal: CurrentPrincipal,
    chat: Chat,
    before: Annotated[uuid.UUID | None, Query(description="Only older than this thread")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> list[ThreadOut]:
    return [ThreadOut.of(t) for t in await chat.threads(principal, before=before, limit=limit)]


@router.get("/threads/{thread_id}", summary="One thread", responses={401: _PROBLEM, 404: _PROBLEM})
async def get_thread(thread_id: uuid.UUID, principal: CurrentPrincipal, chat: Chat) -> ThreadOut:
    return ThreadOut.of(await chat.get_thread(principal, thread_id))


@router.delete(
    "/threads/{thread_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a thread and its messages",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def delete_thread(thread_id: uuid.UUID, principal: CurrentPrincipal, chat: Chat) -> Response:
    await chat.delete_thread(principal, thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/threads/{thread_id}/messages",
    summary="A thread's messages, newest first",
    description="Page with `before`: pass the id of the oldest message you have.",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def list_messages(
    thread_id: uuid.UUID,
    principal: CurrentPrincipal,
    chat: Chat,
    before: Annotated[uuid.UUID | None, Query(description="Only older than this message")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> list[MessageOut]:
    items = await chat.messages(principal, thread_id, before=before, limit=limit)
    return [MessageOut.of(m) for m in items]


@router.post(
    "/threads/{thread_id}/messages",
    response_class=EventSourceResponse,
    summary="Send a message and stream the reply",
    description=(
        "Answers with `text/event-stream`. Events arrive in this order: `turn` once, then "
        "`text` and `tool` as the reply is written, then `done` or `error`. Each event's "
        "data is JSON whose `type` repeats the event name. The reply keeps being written "
        "if the connection closes; reload the messages to get it. A thread runs one reply "
        "at a time (`409`), and `503` means this node cannot reach Claude."
    ),
    responses={401: _PROBLEM, 404: _PROBLEM, 409: _PROBLEM, 422: _PROBLEM, 503: _PROBLEM},
)
async def send_message(
    started: Annotated[StartedTurn, Depends(start_turn)],
) -> AsyncIterator[ChatEvent]:
    yield _sse(
        TurnStartedEvent(
            user_message=MessageOut.of(started.turn.user_message),
            assistant_message=MessageOut.of(started.turn.assistant_message),
        )
    )
    async for event in started.stream:
        if isinstance(event, TextDelta):
            yield _sse(TextEvent(delta=event.text))
        elif isinstance(event, ToolActivity):
            yield _sse(ToolEvent(id=event.id, name=event.name, status=event.status))
        elif isinstance(event, TurnEnded) and event.message is not None:
            message = MessageOut.of(event.message)
            if event.message.status is MessageStatus.COMPLETE:
                yield _sse(DoneEvent(message=message))
            else:
                yield _sse(ErrorEvent(message=message))
