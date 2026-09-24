"""Agent tools of the notifications domain."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from titan.domains.accounts.models import User
from titan.domains.autonomy.models import ActionClass
from titan.domains.autonomy.tools import ToolContext, ToolResult, ToolSpec
from titan.domains.notifications.models import NotificationKind
from titan.domains.notifications.service import BODY_LENGTH, NotificationsService


async def _notify_member(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    username = str(args["username"]).strip().lower()
    recipient = await context.session.scalar(
        select(User).where(User.username == username, User.disabled_at.is_(None))
    )
    sender = await context.session.get(User, context.user_id)
    if recipient is None or sender is None:
        return ToolResult(f"There is no household member called {username}.", is_error=True)
    service = NotificationsService(
        context.session, pusher=context.pusher, push_origins=context.push_origins
    )
    # Commits before pushing, as every notification does; a push cannot be undone.
    notification = await service.notify(
        recipient.id,
        NotificationKind.SYSTEM,
        f"From {sender.display_name}",
        str(args["message"]),
        {"from": sender.username},
    )
    delivered = (
        "and pushed to their phone" if notification.delivered_at else "(no push got through)"
    )
    return ToolResult(f"Sent to {recipient.display_name} {delivered}.")


def _summarize_notify(args: Mapping[str, Any]) -> str:
    return f"Send {args.get('username', '?')} a notification: {args.get('message', '')}"


NOTIFY_MEMBER = ToolSpec(
    domain="notifications",
    name="notify_member",
    description=(
        "Send a short notification to another household member's devices, for "
        "example a reminder or a message. Use list_members to find usernames."
    ),
    action_class=ActionClass.EXTERNAL,
    input_schema={
        "type": "object",
        "properties": {
            "username": {"type": "string", "minLength": 1, "maxLength": 32},
            "message": {"type": "string", "minLength": 1, "maxLength": BODY_LENGTH},
        },
        "required": ["username", "message"],
        "additionalProperties": False,
    },
    run=_notify_member,
    summarize=_summarize_notify,
)

TOOLS = (NOTIFY_MEMBER,)
