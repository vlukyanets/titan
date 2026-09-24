# Plan: M1 token usage per user

Implements the "Token usage tracking per user" item of [M1](../milestones.md)
and the [usage spec](../../spec/domains/usage.md).

Branch `feature/m1-token-usage`, stacked on `feature/m1-policy-approvals`.

- `titan.domains.usage`: an append-only `usage_records` table, recording, and
  monthly totals per user and per model, plus the household view.
- The `chat_turn` reply node records the session's usage from its result
  message before deciding whether the turn succeeded, so failed sessions count.
- `GET /api/v1/usage`, `GET /api/v1/usage/household` (owner only) and
  `titan usage` on a node.
- Budget caps stay in M4; they read the same totals.

Tasks:

- [x] Spec and this plan.
- [x] Usage table, service and migration.
- [ ] Recording from chat turns, successful and failed.
- [ ] API and CLI, OpenAPI regenerated, docs updated.
