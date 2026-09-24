"""Agent tools of the accounts domain."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from titan.domains.accounts.models import User
from titan.domains.autonomy.models import ActionClass
from titan.domains.autonomy.tools import ToolContext, ToolResult, ToolSpec


async def _list_members(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    users = (
        await context.session.scalars(
            select(User).where(User.disabled_at.is_(None)).order_by(User.username)
        )
    ).all()
    lines = [
        f"- {u.username} ({u.display_name}){' (you)' if u.id == context.user_id else ''}"
        for u in users
    ]
    return ToolResult("Household members:\n" + "\n".join(lines))


def _summarize_list(args: Mapping[str, Any]) -> str:
    return "List the household members"


LIST_MEMBERS = ToolSpec(
    domain="accounts",
    name="list_members",
    description="List the members of the household: usernames and display names.",
    action_class=ActionClass.READ,
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    run=_list_members,
    summarize=_summarize_list,
)

TOOLS = (LIST_MEMBERS,)
