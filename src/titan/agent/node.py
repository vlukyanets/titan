"""One Agent SDK session for a graph node (ADR 0002).

Every session gets the model of the node's tier, a custom system prompt, only the
MCP tools the node allows, no built-in Claude Code tools, no settings files and
an empty working directory. Its system/init message is checked against the
selected credential mode before the model is called.
"""

from __future__ import annotations

import enum
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    McpServerConfig,
    Message,
    ResultMessage,
    SystemMessage,
    query,
)
from claude_agent_sdk.types import HookEvent

from titan.agent.auth import AuthMode, ClaudeAuthError, check_init, verify_environment
from titan.settings import Settings


class Tier(enum.StrEnum):
    """Model tiers: fast for classification and parsing, strong for planning and chat."""

    FAST = "fast"
    STRONG = "strong"


class QueryFn(Protocol):
    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]: ...


class AgentRunError(RuntimeError):
    """The session ended without a usable result. Never contains message content."""


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @classmethod
    def of(cls, usage: dict[str, Any] | None) -> TokenUsage:
        usage = usage or {}
        return cls(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        )


@dataclass(frozen=True)
class AgentRun:
    text: str
    session_id: str
    model: str
    num_turns: int
    usage: TokenUsage
    cost_usd: float | None


def model_for(tier: Tier, settings: Settings) -> str:
    return settings.claude_model_strong if tier is Tier.STRONG else settings.claude_model_fast


def agent_options(
    settings: Settings,
    *,
    tier: Tier,
    system_prompt: str,
    mcp_servers: dict[str, McpServerConfig] | None = None,
    allowed_tools: Sequence[str] = (),
    hooks: dict[HookEvent, list[HookMatcher]] | None = None,
    max_turns: int | None = None,
) -> ClaudeAgentOptions:
    workdir = settings.claude_config_dir / "work"
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return ClaudeAgentOptions(
        model=model_for(tier, settings),
        system_prompt=system_prompt,
        # No Bash, file or web tools (ADR 0002); only the node's MCP tools.
        tools=[],
        allowed_tools=list(allowed_tools),
        mcp_servers=mcp_servers or {},
        strict_mcp_config=True,
        hooks=hooks,
        # No user, project or local settings: nothing can add an apiKeyHelper,
        # permissions or CLAUDE.md instructions behind TITAN's back.
        setting_sources=[],
        cwd=workdir,
        env={"CLAUDE_CONFIG_DIR": str(settings.claude_config_dir)},
        max_turns=max_turns,
    )


async def _close(messages: AsyncIterator[Message]) -> None:
    # Closing the SDK's generator stops the Claude Code subprocess.
    close = getattr(messages, "aclose", None)
    if close is not None:
        await close()


def _start(
    prompt: str,
    options: ClaudeAgentOptions,
    settings: Settings,
    query_fn: QueryFn,
    environ: Mapping[str, str] | None,
) -> tuple[AuthMode, AsyncIterator[Message]]:
    mode = AuthMode(settings.claude_auth_mode)
    # The subprocess sees the parent environment with options.env on top.
    parent = os.environ if environ is None else environ
    verify_environment(mode, {**parent, **options.env}, settings.claude_config_dir)
    return mode, query_fn(prompt=prompt, options=options)


async def stream_agent(
    prompt: str,
    options: ClaudeAgentOptions,
    settings: Settings,
    *,
    query_fn: QueryFn = query,
    environ: Mapping[str, str] | None = None,
) -> AsyncIterator[Message]:
    """Yield the session's messages after its credential source has been checked."""
    mode, messages = _start(prompt, options, settings, query_fn, environ)
    checked = False
    try:
        async for message in messages:
            if isinstance(message, SystemMessage) and message.subtype == "init":
                check_init(mode, message)
                checked = True
            elif not checked:
                raise ClaudeAuthError("the session sent messages before its system/init message")
            yield message
    finally:
        await _close(messages)


async def run_agent(
    prompt: str,
    options: ClaudeAgentOptions,
    settings: Settings,
    *,
    query_fn: QueryFn = query,
    environ: Mapping[str, str] | None = None,
) -> AgentRun:
    """Run a session to the end and return its final text and token usage."""
    result: ResultMessage | None = None
    stream = stream_agent(prompt, options, settings, query_fn=query_fn, environ=environ)
    async for message in stream:
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        raise AgentRunError("the session ended without a result")
    if result.is_error:
        status = f", HTTP {result.api_error_status}" if result.api_error_status else ""
        raise AgentRunError(f"the session failed: {result.subtype}{status}")
    return AgentRun(
        text=result.result or "",
        session_id=result.session_id,
        model=options.model or "",
        num_turns=result.num_turns,
        usage=TokenUsage.of(result.usage),
        cost_usd=result.total_cost_usd,
    )


async def self_check(
    settings: Settings,
    *,
    query_fn: QueryFn = query,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Start a session and check its credential source, without calling the model.

    The system/init message arrives before the first API request, so the session is
    closed right after it and costs no tokens. Returns the reported source.
    """
    options = agent_options(settings, tier=Tier.FAST, system_prompt="Reply with OK.", max_turns=1)
    mode, messages = _start("OK?", options, settings, query_fn, environ)
    try:
        async for message in messages:
            if isinstance(message, SystemMessage) and message.subtype == "init":
                return check_init(mode, message)
    finally:
        await _close(messages)
    raise ClaudeAuthError("the session ended without a system/init message")
