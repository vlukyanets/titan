# Plan: M2 tasks and projects

Implements the "Tasks and projects" item of [M2](../milestones.md) and the
[tasks spec](../../spec/domains/tasks.md).

Branch `feature/m2-tasks-projects`, stacked on `refactor/m1-cli-entry-point`.
The agent tools follow on their own branch, so the domain and its API can be
reviewed without waiting for a live agent run.

- `titan.domains.tasks`: `projects`, `project_members` and `tasks` tables;
  access rules and ownership checks in the service; recurrence with
  `python-dateutil`, bounded so a rule that never matches cannot spin.
- REST API under `/api/v1/projects` and `/api/v1/tasks`.
- Agent tools, with the action class raised to `external` for writes to shared
  items.

Tasks:

- [x] Spec and this plan.
- [x] Tables, migration, service and recurrence.
- [x] Projects and tasks API, OpenAPI regenerated, docs updated.
- [ ] Agent tools with audit and undo (separate branch).
