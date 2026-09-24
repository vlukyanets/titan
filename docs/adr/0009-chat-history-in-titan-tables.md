# 0009. Chat history lives in TITAN tables

- Status: Accepted
- Date: 2026-09-24

## Context

A chat turn runs as a LangGraph workflow whose node calls the Claude Agent SDK
([ADR 0002](0002-langgraph-with-agent-sdk-nodes.md)). Later turns need the
conversation so far, and any node of the cluster must be able to run the next
turn. There are three places the conversation could live:

- **Claude Code sessions on local disk**, continued with `resume`. The
  transcript stays on the node that wrote it, so another node cannot continue
  the thread.
- **Claude Code sessions mirrored to a `SessionStore`** in the main database.
  The Agent SDK copies every transcript line to the store and rebuilds a
  temporary local session from it on `resume`. It keeps full fidelity,
  including tool calls and Claude Code's own compaction, but the rows are the
  CLI's internal transcript format, which changes with Claude Code releases,
  and it holds every tool result verbatim.
- **LangGraph checkpoints.** Checkpoints hold serialized graph state, so the
  conversation would sit in opaque blobs that the API cannot page through, and
  message content would end up in every checkpoint.

The API needs the messages anyway, to show a thread's history, and
[ADR 0007](0007-sensitive-data-protection.md) will need to protect content in
places TITAN controls.

## Decision

Chat threads and messages are ordinary domain tables
([chat](../spec/domains/chat.md)), replicated like all other data. Each turn is
a new Agent SDK session. Its prompt holds the thread's latest complete messages
and the new message. Claude Code session transcripts stay in the node's own
Claude configuration directory and are never resumed.

Workflow state and checkpoints carry only ids: the thread, the user message and
the assistant message. The node reads the content from the domain tables when
it runs.

## Consequences

- Any node can run any turn, and the API pages through history with plain
  queries.
- Message content has one home, so ADR 0007 has one place to protect.
- A later turn sees earlier replies, but not the tool calls and results behind
  them. If the model needs a task id from three turns ago, it looks the task up
  again. Replies should therefore name what they refer to.
- Long threads are cut to the latest messages instead of being compacted. A
  summary of older messages can be added later without changing the tables.
- Switching to `SessionStore`-backed sessions later stays possible: the domain
  tables would remain the user-facing history, and the store would be an
  extra.
