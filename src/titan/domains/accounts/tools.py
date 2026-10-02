"""Agent tools of the accounts domain."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.accounts.models import User
from titan.domains.autonomy.models import ActionClass
from titan.domains.autonomy.tools import ToolContext, ToolResult, ToolSpec


async def member_ids(
    session: AsyncSession, usernames: Iterable[object], error: type[Exception]
) -> list[uuid.UUID]:
    """Ids of active members by username, as tools name them; raises `error`."""
    wanted = [str(u).strip().lower() for u in usernames]
    if not wanted:
        return []
    rows = await session.execute(
        select(User.username, User.id).where(User.username.in_(wanted), User.disabled_at.is_(None))
    )
    found = dict(rows.tuples().all())
    for username in wanted:
        if username not in found:
            raise error(f"there is no household member called {username}")
    return [found[u] for u in wanted]


async def member_names(session: AsyncSession, ids: Iterable[uuid.UUID]) -> list[str]:
    ids = list(ids)
    if not ids:
        return []
    rows = await session.execute(select(User.id, User.username).where(User.id.in_(ids)))
    names = dict(rows.tuples().all())
    return [names.get(i, "?") for i in ids]


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
