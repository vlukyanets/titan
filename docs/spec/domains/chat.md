# Domain: chat

Status: **Draft v1**. Part of the [product spec](../product.md). Chat is how a
user talks to the assistant from any surface. Domain tools, approvals and
memory plug into it as they arrive.

## Entities

| Entity | Key fields |
|---|---|
| `ChatThread` | id, user, title, created_at, updated_at |
| `ChatMessage` | id, thread, role (`user`, `assistant`), content, status (`complete`, `streaming`, `failed`), model?, token usage?, cost?, created_at, completed_at? |

- A thread belongs to one user. Its title is the start of its first message,
  so naming a thread costs no model call.
- `updated_at` moves with every new message, so the thread list shows recent
  conversations first.
- An assistant message records the model, its token usage (input, output,
  cache reads, cache writes) and the cost Claude Code reports. Usage tracking
  per user and the monthly budget build on these numbers.

## Turns

A turn is one user message and the assistant's reply to it.

- The user message is stored first. Then an assistant message is stored with
  status `streaming`, and the agent runs the `chat_turn` workflow
  ([ADR 0002](../../adr/0002-langgraph-with-agent-sdk-nodes.md)).
- The reply streams to the client as it is written. When the turn ends, the
  assistant message holds the full reply and becomes `complete`. A turn that
  fails becomes `failed`, with an error that contains no message content.
- A thread runs at most one turn at a time. Sending while a turn is running
  answers `409`.
- A turn keeps running when the client disconnects. The reply is saved, and
  the client reloads the thread's messages to show it. Resuming a live stream
  after a disconnect is not supported in v1.
- A turn has a time limit (`TITAN_CHAT_TURN_TIMEOUT_SECONDS`, 5 minutes by
  default). A message still `streaming` after twice the limit, for example
  because its node crashed, counts as `failed`, so the thread is never stuck.
- Each turn is a new Claude session. The agent receives the thread's latest
  complete messages (`TITAN_CHAT_HISTORY_MESSAGES`, 40 by default) and the new
  message ([ADR 0009](../../adr/0009-chat-history-in-titan-tables.md)). Failed
  replies are left out.
- The assistant answers in the language of the user's message. English,
  Russian and Ukrainian are expected, but any language works.
- Chat uses the `strong` model tier. The budget fallback to the `fast` tier
  arrives with the monthly budget.
- An approved action's result is added to the thread as an assistant message
  without a model, so a thread can gain messages outside a turn.
- Message contents are never written to logs, workflow checkpoints or error
  messages.

## Streaming

`POST /api/v1/chat/threads/{id}/messages` answers with `text/event-stream`.
Every event's data is a JSON object whose `type` repeats the event name:

| Event | Data | When |
|---|---|---|
| `turn` | `user_message`, `assistant_message` (the stored messages) | First, once both messages are stored |
| `text` | `delta` | Each piece of the reply as it is written |
| `tool` | `id`, `name`, `status` (`started`, `finished`, `failed`) | A domain tool call starts or ends |
| `approval` | `approval` (the stored request) | The agent asked for the user's approval ([autonomy](autonomy.md)) |
| `done` | `message` (the complete assistant message) | Last, on success |
| `error` | `message` (the failed assistant message) | Last, on failure |

The stored reply is the model's final answer. Text the model writes before a
tool call streams too but may not be part of it, so a client shows the `done`
message's content in place of the streamed text. The stream sends a comment
line every 15 seconds while the model is silent, so proxies keep the connection
open.

## API

| Call | Purpose |
|---|---|
| `POST /api/v1/chat/threads` | Start a thread |
| `GET /api/v1/chat/threads` | The caller's threads, most recently active first; `before` and `limit` |
| `GET /api/v1/chat/threads/{id}` | One thread |
| `DELETE /api/v1/chat/threads/{id}` | Delete a thread and its messages |
| `GET /api/v1/chat/threads/{id}/messages` | Messages, newest first; `before` and `limit` |
| `POST /api/v1/chat/threads/{id}/messages` | Send a message and stream the reply |

A user sees only their own threads. Someone else's thread id answers `404`.
When the node has no working Claude credential, sending a message answers
`503` and everything else keeps working.

## Acceptance criteria (v1)

- A message sent from the CLI or the Android app gets a reply that streams in
  pieces and ends with `done`.
- The stored assistant message holds the full reply, its model and its token
  usage.
- A second message sent while a turn is running answers `409`.
- Closing the stream in the middle of a turn does not lose the reply.
- The agent remembers what was said earlier in the same thread, and nothing
  from other threads.
- No workflow checkpoint contains message content.
