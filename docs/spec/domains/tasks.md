# Domain: tasks and projects

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

## Entities

| Entity | Key fields |
|---|---|
| `Project` | id, owner, title, description, status (`active`, `archived`), shared_with |
| `Task` | id, owner, project_id?, title, notes, status (`todo`, `doing`, `done`, `cancelled`), priority (1–4), due_at?, estimate_minutes?, recurrence (RRULE)?, tags, created_at, completed_at? |

Completing a recurring task creates the next occurrence from its RRULE.

## Agent tools

| Tool | Action class |
|---|---|
| `tasks.list` / `tasks.search` / `tasks.get` | `read` |
| `tasks.create` / `tasks.update` / `tasks.complete` | `write-internal` |
| `projects.create` / `projects.update` / `projects.archive` | `write-internal` |
| `tasks.delete` / `projects.delete` | `destructive` |
| Any write to an item shared with another user | `external` |

## Acceptance criteria (v1)

- A user can create, edit, complete and delete tasks and projects from every
  surface and through the agent ("remind me to renew the passport next month").
- Recurring tasks roll over correctly on completion.
- Tasks with `due_at` and `estimate_minutes` are visible to the
  [calendar planner](calendar.md).
- Every agent write appears in the audit log and can be undone.
