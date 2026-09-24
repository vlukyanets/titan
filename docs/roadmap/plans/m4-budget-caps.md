# Plan: M4 monthly budget caps

Implements the "Monthly budget caps" item of [M4](../milestones.md) and the
[budget section of the usage spec](../../spec/domains/usage.md#monthly-budget).

Branch `feature/m4-budget-caps`, stacked on `feature/m2-notes`.

- `titan.domains.usage`: a `budgets` table, one row per capped user, and a
  budget service that works out the state from the month's cost and sends the
  `budget` notifications with deterministic ids.
- The agent layer checks the budget after it records a session's usage, and the
  `chat_turn` reply node picks the `fast` tier while the budget is exceeded.
- Owner alerts: a per-budget setting (`off`, `exceeded`, `all`) for whether
  owners are notified too.
- Scheduled workflows do not exist yet. The common runner that will start them
  checks `BudgetService.status()` before each run and notifies the user of a
  skipped run; that lands with the first workflow (`daily_plan`, see the
  [calendar plan](m2-calendar.md)).
- API under `/api/v1/usage`, `titan budget list|set` on a node.

Tasks:

- [x] Spec and this plan.
- [x] Budget table, service, notifications and migration.
- [x] Chat falls back to the `fast` tier; usage recording checks the budget.
- [x] API and CLI, OpenAPI regenerated, docs updated.
- [ ] Owner alerts: spec, column, notifications, API and CLI.
- [ ] Budget check and skipped-run notice in the workflow runner (with the first
      scheduled workflow).
