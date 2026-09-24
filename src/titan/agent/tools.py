"""The registry of domain tools and the one path that runs them (ADR 0005, ADR 0010).

The model reaches tools through in-process SDK MCP servers, one per domain; an
approval runs a stored call directly. Both go through `execute`, which checks
the input against the tool's schema, runs it, and commits the change together
with its audit entry.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import jsonschema
from claude_agent_sdk import McpServerConfig, SdkMcpTool, create_sdk_mcp_server
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from titan.domains.accounts import tools as accounts_tools
from titan.domains.autonomy.models import ActionClass, Approval, Decision
from titan.domains.autonomy.service import ApprovalsService, AuditService, PolicyService
from titan.domains.autonomy.tools import ToolContext, ToolResult, ToolSpec
from titan.domains.chat import tools as chat_tools
from titan.domains.chat.service import ChatService
from titan.domains.notifications import tools as notifications_tools
from titan.notify import Pusher

log = logging.getLogger(__name__)

REGISTRY: Mapping[str, ToolSpec] = {
    spec.qualified_name: spec
    for spec in (*accounts_tools.TOOLS, *chat_tools.TOOLS, *notifications_tools.TOOLS)
}


@dataclass(frozen=True)
class ToolScope:
    """Who the tools of one session or one approval act for."""

    sessions: async_sessionmaker[AsyncSession]
    user_id: uuid.UUID
    thread_id: uuid.UUID | None = None
    pusher: Pusher | None = None
    push_origins: tuple[str, ...] = ()

    def context(self, session: AsyncSession) -> ToolContext:
        return ToolContext(session, self.user_id, self.thread_id, self.pusher, self.push_origins)


@dataclass(frozen=True)
class Execution:
    result: ToolResult
    audit_entry_id: uuid.UUID | None = None


async def execute(
    spec: ToolSpec,
    scope: ToolScope,
    tool_input: dict[str, Any],
    *,
    decision: Decision,
    approval_id: uuid.UUID | None = None,
) -> Execution:
    """Run one call and record it. Never raises for a failing tool."""
    try:
        jsonschema.validate(tool_input, spec.input_schema)
    except jsonschema.ValidationError as exc:
        return Execution(ToolResult(f"Invalid input: {exc.message}", is_error=True))
    async with scope.sessions() as session:
        try:
            result = await spec.run(scope.context(session), tool_input)
            entry = None
            if not result.is_error and spec.action_class is not ActionClass.READ:
                entry = AuditService(session).record(
                    scope.user_id,
                    spec,
                    tool_input,
                    decision,
                    result.change,
                    approval_id=approval_id,
                )
            if result.is_error:
                await session.rollback()
            else:
                await session.commit()
        except Exception as exc:
            await session.rollback()
            # The type only: messages of database errors can carry user data.
            log.error("tool %s failed: %s", spec.qualified_name, type(exc).__name__)
            return Execution(ToolResult("The tool failed.", is_error=True))
        return Execution(result, entry.id if entry is not None else None)


def _handler(
    spec: ToolSpec, scope: ToolScope
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def handle(args: dict[str, Any]) -> dict[str, Any]:
        async with scope.sessions() as session:
            decision = await PolicyService(session).decide(
                scope.user_id, spec.domain, spec.action_class
            )
        # The PreToolUse hook decides; this only refuses what it should have
        # stopped, in case a call ever reaches a tool without it.
        if decision in (Decision.CONFIRM, Decision.DENY):
            log.error("tool %s reached without the policy hook", spec.qualified_name)
            text, is_error = "This action is not allowed without the user's approval.", True
        else:
            execution = await execute(spec, scope, args, decision=decision)
            text, is_error = execution.result.text, execution.result.is_error
        return {"content": [{"type": "text", "text": text}], "is_error": is_error}

    return handle


def mcp_servers(scope: ToolScope) -> dict[str, McpServerConfig]:
    """One in-process MCP server per domain, bound to the scope's user and thread."""
    by_domain: dict[str, list[SdkMcpTool[Any]]] = {}
    for spec in REGISTRY.values():
        by_domain.setdefault(spec.domain, []).append(
            SdkMcpTool(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema,
                handler=_handler(spec, scope),
            )
        )
    servers: dict[str, McpServerConfig] = {
        domain: create_sdk_mcp_server(name=domain, tools=tools)
        for domain, tools in by_domain.items()
    }
    return servers


def result_note(approval: Approval, result: ToolResult) -> str:
    """The assistant message posted to the thread once an approved call ran."""
    outcome = "Could not do it" if result.is_error else "Done"
    return f"Approved: {approval.summary}\n{outcome}: {result.text}"


async def run_approved(approval: Approval, scope: ToolScope) -> Approval:
    """Run a claimed approval's stored call, record the outcome and tell the thread."""
    spec = REGISTRY.get(approval.tool)
    if spec is None:
        execution = Execution(ToolResult("This tool no longer exists.", is_error=True))
    else:
        execution = await execute(
            spec, scope, dict(approval.input), decision=Decision.CONFIRM, approval_id=approval.id
        )
    result = execution.result
    async with scope.sessions() as session:
        finished = await ApprovalsService(session).finish(
            approval.id,
            ok=not result.is_error,
            result=result.text,
            audit_entry_id=execution.audit_entry_id,
        )
        if approval.thread_id is not None:
            await ChatService(session).post_note(
                approval.user_id, approval.thread_id, result_note(approval, result)
            )
    return finished or approval
