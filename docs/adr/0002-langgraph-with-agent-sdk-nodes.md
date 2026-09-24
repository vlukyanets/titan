# 0002. LangGraph workflows with Claude Agent SDK nodes

- Status: Accepted
- Date: 2026-09-24

## Context

TITAN has two kinds of agent work:

- **Fixed workflows** with a known shape: daily planning, replanning, weekly
  review, memory extraction. They need branching, pauses for human approval,
  and durable checkpoints so any cluster node can resume them.
- **Open-ended reasoning** inside those steps and in chat: understand a request,
  choose domain tools, call them, and answer.

The product is meant to be built on Claude Code technology. The Claude Agent SDK
gives us the Claude Code agent loop, in-process MCP tools, subagents, hooks for
permission gating, sessions, and the same credential handling as Claude Code
([ADR 0003](0003-claude-auth-modes.md)).

## Options

1. **Agent SDK only.** Fewer moving parts. Workflow state, interrupts and
   checkpoints would have to be written by hand.
2. **LangGraph only** through `langchain-anthropic`. Good workflow tooling, but
   it loses the Claude Code harness, and API-key auth is the only option.
3. **LangGraph as the outer layer, Agent SDK inside nodes.** Each framework does
   what it is good at. The cost is two frameworks and a boundary between them.

## Decision

Option 3. LangGraph `StateGraph`s define every workflow, including a single chat
turn. A graph node that needs reasoning calls the Agent SDK `query()` with
options set by that node: model tier, the domain MCP servers it may use, the
`PreToolUse` policy hook and the `PostToolUse` audit hook.

- The LangGraph checkpointer stores its data in the main replicated database.
- An approval request raised by the policy hook becomes a LangGraph `interrupt`.
  The graph resumes on the user's answer.
- Built-in Claude Code tools (Bash, file editing, web access) are not allowed in
  TITAN agent nodes unless an ADR says otherwise.
- LangChain chat-model wrappers around Claude Code are **not** used. Nodes call
  the Agent SDK directly, so tool calls stay visible to hooks and the audit log.

## Consequences

- The M0 spike implements `daily_plan` end to end to prove the boundary: tool
  calls, interrupt and resume, token accounting, and a checkpoint resumed on
  another node.
- Streaming to clients has to combine LangGraph events with Agent SDK messages
  into one SSE event stream.
- Both libraries are pinned, and upgrades are tested with the workflow suite.
