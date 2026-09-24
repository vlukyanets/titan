# Domain: usage

Status: **Draft v1**. Part of the [product spec](../product.md#cost-control).
Records what every agent session cost, per user, so people can see it and the
monthly budget can build on it.

## Entities

| Entity | Key fields |
|---|---|
| `UsageRecord` | id, user, source, reference?, model, input tokens, output tokens, cache reads, cache writes, cost?, created_at |

- One record per Agent SDK session that reported usage, whether it succeeded or
  failed: a failed session can still have used tokens.
- `source` names what ran the session: `chat` for a chat turn, later the name of
  a workflow such as `daily_plan`. `reference` points at what the session
  produced, for a chat turn the assistant message.
- The cost is the figure Claude Code reports. In `oauth` mode it is what the
  same tokens would cost on the API; the subscription itself is billed
  separately.
- Records are only added, never changed. They hold numbers, not content.
- Months are calendar months in UTC.

## API

| Call | Purpose |
|---|---|
| `GET /api/v1/usage?month=YYYY-MM` | The caller's totals for a month (the current one by default), overall and per model |
| `GET /api/v1/usage/household?month=YYYY-MM` | Every user's totals for a month (owner only) |

A month that is not `YYYY-MM` answers `422`. Members see only their own usage.

On a node, `titan usage [--month YYYY-MM]` prints every user's totals.

## What builds on it

The monthly budget per user (warnings at 80 %, the fallback to the `fast` tier
at 100 %) is milestone M4 and reads these totals.

## Acceptance criteria (v1)

- After a chat turn, the caller's usage for the month includes its tokens and
  cost, under the model that answered.
- A turn that fails after the model was called still counts.
- The owner sees every member's totals; a member asking for them gets `403`.
