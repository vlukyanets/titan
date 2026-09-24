"""How a domain declares an agent tool (ADR 0005, ADR 0010).

Each domain lists its tools in its own `tools.py` as `ToolSpec`s. The agent
runtime turns them into in-process MCP servers and runs them through one
wrapper that writes the audit log; approvals run the same specs.

Contract for `run`: use `context.session` and flush, but do not commit. The
wrapper commits the change together with its audit entry. A tool with an effect
outside the database, such as a push, may commit before causing it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.autonomy.models import ActionClass
from titan.notify import Pusher


@dataclass(frozen=True)
class ToolContext:
    """Who a tool acts for, and what it may use."""

    session: AsyncSession
    user_id: uuid.UUID
    thread_id: uuid.UUID | None = None
    pusher: Pusher | None = None
    push_origins: tuple[str, ...] = ()


@dataclass(frozen=True)
class Change:
    """One entity's state before and after a call, enough to undo it."""

    entity_type: str
    entity_id: str
    before: dict[str, Any]
    after: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """What the model is told, and what changed."""

    text: str
    change: Change | None = None
    is_error: bool = False


RunFn = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]
# Restores `change.before`; raises UndoConflictError if the entity no longer
# has `change.after`.
UndoFn = Callable[[ToolContext, Change], Awaitable[None]]


@dataclass(frozen=True)
class ToolSpec:
    domain: str
    name: str
    description: str
    action_class: ActionClass
    input_schema: dict[str, Any]
    run: RunFn
    # A line a person can approve without seeing the raw input.
    summarize: Callable[[Mapping[str, Any]], str]
    undo: UndoFn | None = None

    @property
    def qualified_name(self) -> str:
        """The name Claude Code gives the tool of an SDK MCP server."""
        return f"mcp__{self.domain}__{self.name}"
