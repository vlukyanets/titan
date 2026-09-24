"""Agent tools of the chat domain."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from titan.domains.autonomy.errors import UndoConflictError
from titan.domains.autonomy.models import ActionClass
from titan.domains.autonomy.tools import Change, ToolContext, ToolResult, ToolSpec
from titan.domains.chat.models import ChatThread
from titan.domains.chat.service import TITLE_LENGTH, title_from


async def _own_thread(context: ToolContext, thread_id: uuid.UUID | None) -> ChatThread | None:
    if thread_id is None:
        return None
    thread: ChatThread | None = await context.session.scalar(
        select(ChatThread)
        .where(ChatThread.id == thread_id, ChatThread.user_id == context.user_id)
        .with_for_update()
    )
    return thread


async def _rename(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    thread = await _own_thread(context, context.thread_id)
    if thread is None:
        return ToolResult("There is no conversation to rename here.", is_error=True)
    before, title = thread.title, title_from(str(args["title"]))
    if not title:
        return ToolResult("The title is empty.", is_error=True)
    thread.title = title
    await context.session.flush()
    return ToolResult(
        f"The conversation is now called “{title}”.",
        Change("chat_thread", str(thread.id), {"title": before}, {"title": title}),
    )


async def _undo_rename(context: ToolContext, change: Change) -> None:
    thread = await _own_thread(context, uuid.UUID(change.entity_id))
    if thread is None:
        raise UndoConflictError("the conversation no longer exists")
    if thread.title != change.after["title"]:
        raise UndoConflictError("the conversation was renamed again since")
    thread.title = str(change.before["title"])
    await context.session.flush()


def _summarize_rename(args: Mapping[str, Any]) -> str:
    return f"Rename this conversation to “{title_from(str(args.get('title', '')))}”"


RENAME_THREAD = ToolSpec(
    domain="chat",
    name="rename_thread",
    description="Rename the current conversation, as it appears in the user's thread list.",
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": {"title": {"type": "string", "minLength": 1, "maxLength": TITLE_LENGTH}},
        "required": ["title"],
        "additionalProperties": False,
    },
    run=_rename,
    summarize=_summarize_rename,
    undo=_undo_rename,
)

TOOLS = (RENAME_THREAD,)
