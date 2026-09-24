"""A stand-in for the Claude Code CLI that uses tools the way the CLI does.

For every scripted call it runs the session's PreToolUse hooks and, if they
allow it, calls the tool on the session's in-process MCP server over JSON-RPC,
through the Agent SDK's own bridge. Nothing reaches a model.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge


@dataclass
class Outcome:
    tool: str
    decision: str
    reason: str
    result: str | None = None
    is_error: bool = False


@dataclass
class FakeClaude:
    """Calls `calls` in order, then answers `reply`."""

    calls: list[tuple[str, dict[str, Any]]]
    reply: str = "Done."
    outcomes: list[Outcome] = field(default_factory=list)
    options: list[ClaudeAgentOptions] = field(default_factory=list)

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.options.append(options)
        return self._run(options)

    async def _run(self, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        yield SystemMessage(subtype="init", data={"apiKeySource": "ANTHROPIC_API_KEY"})
        bridges: dict[str, SdkMcpBridge] = {}
        ids = itertools.count(1)
        try:
            for name, tool_input in self.calls:
                use_id = f"toolu_{uuid.uuid4().hex[:8]}"
                yield AssistantMessage(
                    content=[ToolUseBlock(id=use_id, name=name, input=tool_input)], model="m"
                )
                outcome = await self._pre_tool_use(options, name, tool_input, use_id)
                if outcome.decision == "allow":
                    await self._call(options, bridges, ids, name, tool_input, outcome)
                self.outcomes.append(outcome)
                text = outcome.result if outcome.result is not None else outcome.reason
                yield UserMessage(
                    content=[
                        ToolResultBlock(
                            tool_use_id=use_id,
                            content=text,
                            is_error=outcome.decision != "allow" or outcome.is_error,
                        )
                    ]
                )
            yield AssistantMessage(content=[TextBlock(text=self.reply)], model="m")
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=len(self.calls) + 1,
                session_id="s1",
                result=self.reply,
                usage={"input_tokens": 10, "output_tokens": 5},
            )
        finally:
            for bridge in bridges.values():
                await bridge.aclose()

    async def _pre_tool_use(
        self, options: ClaudeAgentOptions, name: str, tool_input: dict[str, Any], use_id: str
    ) -> Outcome:
        # Without an allowing hook, Claude Code's own permission check refuses a
        # tool that is not in allowed_tools.
        decision = "allow" if name in options.allowed_tools else "deny"
        reason = "not allowed"
        for matcher in (options.hooks or {}).get("PreToolUse", []):
            for hook in matcher.hooks:
                answer = await hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": name,
                        "tool_input": tool_input,
                        "tool_use_id": use_id,
                        "session_id": "s1",
                        "transcript_path": "",
                        "cwd": "",
                    },
                    use_id,
                    {"signal": None},
                )
                raw: Any = answer.get("hookSpecificOutput")
                specific: dict[str, Any] = raw or {}
                if specific.get("permissionDecision"):
                    decision = specific["permissionDecision"]
                    reason = specific.get("permissionDecisionReason", "")
        return Outcome(name, decision, reason)

    async def _call(
        self,
        options: ClaudeAgentOptions,
        bridges: dict[str, SdkMcpBridge],
        ids: Any,
        name: str,
        tool_input: dict[str, Any],
        outcome: Outcome,
    ) -> None:
        _, server, tool = name.split("__", 2)
        config: Any = options.mcp_servers[server]  # type: ignore[index]
        bridge = bridges.get(server)
        if bridge is None:
            bridge = bridges[server] = SdkMcpBridge(server, config["instance"])
            await bridge.handle(
                {
                    "jsonrpc": "2.0",
                    "id": next(ids),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "fake-claude", "version": "0"},
                    },
                }
            )
            await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": next(ids),
                "method": "tools/call",
                "params": {"name": tool, "arguments": tool_input},
            }
        )
        assert response is not None
        result = response["result"]
        outcome.result = "".join(c.get("text", "") for c in result["content"])
        outcome.is_error = bool(result.get("isError"))
