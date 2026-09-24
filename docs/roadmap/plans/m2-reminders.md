# Plan: M2 reminders

Implements the "Reminders with exactly-once firing" item of
[M2](../milestones.md) and the [reminders spec](../../spec/domains/reminders.md).

Branch `feature/m2-reminders`, stacked on `feature/m2-tasks-projects`.

- `titan.domains.reminders`: the `reminders` table, the lifecycle (fire,
  snooze, dismiss, reschedule), recurrence shared with tasks, and firing that
  changes the status in the same transaction as the notification.
- REST API under `/api/v1/reminders`, including the notification's Snooze and
  Done actions.
- The scheduler follows on its own branch: a `jobs` table with leases and a
  preferred node (ADR 0006), the `titan-worker` entry point that fires due
  reminders, and its Compose service. That branch also finishes the M1 item for
  the worker image.
- Default reminders for tasks with a due time, and the agent tools, come after
  the scheduler.

Tasks:

- [x] Spec and this plan.
- [ ] Reminders table, service and firing.
- [ ] Reminders API, OpenAPI regenerated, docs updated.
- [ ] Scheduler with leases and the `titan-worker` entry point (separate branch).
- [ ] Default reminders for tasks, agent tools.
