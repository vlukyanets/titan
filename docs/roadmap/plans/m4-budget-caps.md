# Plan: M4 monthly budget caps

Implements the "Monthly budget caps" item of [M4](../milestones.md) and the
[budget section of the usage spec](../../spec/domains/usage.md#monthly-budget).

Branch `feature/m4-budget-caps`, stacked on `feature/m2-notes`.

- `titan.domains.usage`: a `budgets` table, one row per capped user, and a
  budget service that works out the state from the month's cost and sends the
  `budget` notifications with deterministic ids.
- The agent layer checks the budget after it records a session's usage, and the
  `chat_turn` reply node picks the `fast` tier while the budget is exceeded.
- Scheduled workflows do not exist yet. The first one (`daily_plan`) checks
  `BudgetService.status()` before it starts; the service already answers it.
- API under `/api/v1/usage`, `titan budget list|set` on a node.

Tasks:

- [x] Spec and this plan.
- [ ] Budget table, service, notifications and migration.
- [ ] Chat falls back to the `fast` tier; usage recording checks the budget.
- [ ] API and CLI, OpenAPI regenerated, docs updated.
