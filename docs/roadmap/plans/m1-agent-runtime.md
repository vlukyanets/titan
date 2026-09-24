# Plan: M1 agent runtime

Implements the "Agent runtime" item of [M1](../milestones.md):
[ADR 0002](../../adr/0002-langgraph-with-agent-sdk-nodes.md) (LangGraph with
Agent SDK nodes) and [ADR 0003](../../adr/0003-claude-auth-modes.md) with its
rules in [claude-auth.md](../../architecture/claude-auth.md). No Claude
credential is available to the development sessions yet, so everything is
tested against a fake SDK, and the live checks wait for one.

## Stage 1: auth modes, model tiers, SDK node wrapper

Branch `feature/m1-agent-runtime`, stacked on the notifications branch.

- `titan.agent.auth`: builds the SDK subprocess environment for the selected
  mode. It keeps exactly one credential (`ANTHROPIC_API_KEY` or
  `CLAUDE_CODE_OAUTH_TOKEN`), removes every other `ANTHROPIC_*` variable, the
  other mode's credential and all cloud provider switches, and sets
  `CLAUDE_CONFIG_DIR` to a dedicated directory. Because the SDK merges its
  `env` option over `os.environ`, the cleaned environment replaces
  `os.environ` in the agent process before any SDK call.
- Startup self-check: one minimal session, then the `apiKeySource` of its
  `system/init` message must be exactly `ANTHROPIC_API_KEY` in `api-key` mode
  and `none` in `oauth` mode. The values come from the credential resolution in
  the bundled Claude Code 2.1.281 (`ANTHROPIC_API_KEY`, `apiKeyHelper`,
  `/login managed key`, `none`; an OAuth token is not an API key source).
  `titan claude check` runs it by hand.
- Model tiers `fast` and `strong`, mapped to model ids by
  `TITAN_CLAUDE_MODEL_FAST` and `TITAN_CLAUDE_MODEL_STRONG`.
- `titan.agent.node`: runs one Agent SDK session for a graph node with the tier's
  model, a custom system prompt, only the MCP tools the node allows, no built-in
  Claude Code tools, no settings sources, and an empty working directory. It
  streams the SDK messages and returns the final text with token usage.

Tasks:

- [x] Settings: auth mode, config directory, tier models.
- [x] Environment cleaning with tests that start from a polluted environment in
      both modes.
- [x] `system/init` check, self-check, `titan claude check`, the `oauth`
      warning.
- [x] Node wrapper with tiers and usage, tested with a fake `query`.
- [x] `claude-auth.md` records the expected `apiKeySource` values.
- [x] `titan claude check` against the bundled Claude Code with fake
      credentials in both modes, as a test.
- [ ] Live: one real session per mode once a credential is available
      (blocked).

## Stage 2: the chat turn and the chat API

Branch `feature/m1-chat-turn`, stacked on stage 1. Implements the
[chat spec](../../spec/domains/chat.md) and
[ADR 0009](../../adr/0009-chat-history-in-titan-tables.md), and with it the
"Chat API with SSE streaming" item of M1.

- `titan.domains.chat`: threads and messages, ownership checks, the one-turn
  rule with stale `streaming` messages counted as failed, and the history
  window for a turn.
- LangGraph checkpoint tables in the main database, created by an Alembic
  revision instead of `AsyncPostgresSaver.setup()`, so every schema change
  stays an Alembic revision. A test runs `setup()` in a scratch schema and
  compares the result, so a LangGraph upgrade that changes the tables fails
  CI.
- `titan.agent.chat`: the `chat_turn` graph. State holds only ids. The `reply`
  node loads the history, runs one Agent SDK session with partial messages on,
  and turns SDK messages into `text` and `tool` events through the LangGraph
  stream writer. The turn's checkpoints are deleted when it ends.
- The API process runs turns itself until `titan-worker` exists. A turn runs
  in a task of its own, so a closed connection does not stop it.
- Chat routes with SSE through FastAPI's `EventSourceResponse`, keep-alive
  comments, `503` when the Claude credential is missing, and the event types in
  the OpenAPI schema.
- LangSmith tracing is switched off: agent startup removes every
  `LANGSMITH_*` and `LANGCHAIN_*` variable next to the credential cleaning, so
  conversations never leave the cluster.

Tasks:

- [x] Spec, ADR 0009 and this plan.
- [x] Chat tables, service and migration, with the checkpoint tables.
- [ ] `chat_turn` graph with the Postgres checkpointer, tested with a fake
      `query`.
- [ ] Chat API with SSE, OpenAPI regenerated, docs updated.
- [ ] Live: one real chat turn once a credential is available (blocked).
