# Domain: usage

Status: **Draft v1**. Part of the [product spec](../product.md#cost-control).
Records what every agent session cost, per user, so people can see it, and
caps it with a monthly budget.

## Entities

| Entity | Key fields |
|---|---|
| `UsageRecord` | id, user, source, reference?, model, input tokens, output tokens, cache reads, cache writes, cost?, created_at |
| `Budget` | user, monthly limit in USD, updated_at |

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
| `GET /api/v1/usage/budget` | The caller's budget for the current month: limit, spent, state |
| `GET /api/v1/usage/budgets` | Every user's budget for the current month (owner only) |
| `PUT /api/v1/usage/budgets/{user_id}` | Set or remove a user's monthly limit (owner only) |

A month that is not `YYYY-MM` answers `422`. Members see only their own usage
and budget.

On a node, `titan usage [--month YYYY-MM]` prints every user's totals,
`titan budget list` every user's budget and `titan budget set USERNAME
AMOUNT|none` sets or removes a limit.

## Monthly budget

- The owner gives any user, themselves included, a monthly limit in US
  dollars: from 0 to 100 000, rounded to cents. A user without a limit has no
  cap. Members cannot change their own limit.
- What counts against it is the month's cost as defined above. Sessions that
  reported no cost add nothing.
- The state is `ok` below 80 % of the limit, `warning` from 80 % and
  `exceeded` from 100 %. A limit of 0 is always `exceeded`.
- When a session or a new limit puts the user at `warning` or `exceeded`, they
  get a `budget` notification. Each state notifies once per month and limit: the
  notification id is derived from the user, month, state and limit, so nodes
  that record at the same time store one notification, although each may push
  it (ADR 0006).
  Raising or lowering the limit re-arms both.
- While `exceeded`, chat turns run on the `fast` tier and scheduled workflows
  do not start. Both check the state when they begin, so a session already
  running finishes on its model. The owner raising the limit, or the next
  month, lifts it.

## Acceptance criteria (v1)

- After a chat turn, the caller's usage for the month includes its tokens and
  cost, under the model that answered.
- A turn that fails after the model was called still counts.
- The owner sees every member's totals; a member asking for them gets `403`.
- A member at 80 % of their limit gets one `budget` notification, however many
  sessions follow, and one more at 100 %.
- A member over their limit gets chat replies from the `fast` tier; after the
  owner raises the limit, the next turn uses the `strong` tier again.
- A member setting a limit gets `403`; a negative limit answers `422`.
