# Domain: tasks and projects

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

## Entities

| Entity | Key fields |
|---|---|
| `Project` | id, owner, title, description, status (`active`, `archived`), shared_with, created_at, updated_at |
| `Task` | id, owner, project_id?, title, notes, status (`todo`, `doing`, `done`, `cancelled`), priority (1–4), due_at?, estimate_minutes?, recurrence (RRULE)?, tags, created_at, updated_at, completed_at? |

- Priority 1 is the most urgent, 4 the least; new tasks get 4.
- A task without a project is in its owner's inbox.
- Tags are short lowercase labels (at most 20 per task, 32 characters each).

## Sharing and access

- A project is shared by listing members in `shared_with`. Only the project's
  owner changes that list, renames, archives or deletes it.
- Everyone the project is shared with sees it and its tasks, adds tasks to it,
  and edits, completes and reopens any of its tasks.
- A task is always visible to its owner, even after its project stops being
  shared with them. Tasks outside a shared project are private.
- A task is deleted by its owner or by its project's owner.
- Deleting a project moves its tasks to their owners' inboxes rather than
  deleting them, so no member loses their work. Archiving keeps them in place.
- Tasks cannot be added to an archived project.
- Anything a user has no access to answers `404`, as if it did not exist.

## Recurrence

- `recurrence` is an RFC 5545 RRULE without `DTSTART`, such as
  `FREQ=WEEKLY;BYDAY=MO`. `FREQ` is `DAILY`, `WEEKLY`, `MONTHLY` or `YEARLY`;
  `COUNT` is not accepted (use `UNTIL`). A recurring task needs `due_at`, which
  is the rule's start.
- Completing a recurring task marks it `done` and creates the next occurrence:
  a copy with status `todo` whose `due_at` is the rule's first occurrence after
  both the old `due_at` and the moment of completion. Missed occurrences are
  skipped, not piled up. When the rule has ended there is no next occurrence.
- The rule moves to the new occurrence, so the completed one no longer
  recurs: reopening and completing it again creates no second copy.
- Rules are evaluated in UTC for now, so a local time of day can shift by an
  hour across daylight saving changes (open question 8 in the roadmap).

## Status changes

- `POST /tasks/{id}/complete` is the only way to `done`; it sets
  `completed_at` and rolls a recurring task over. Completing a task that is
  already `done` answers `409`.
- Setting the status to `todo` or `doing` reopens a task and clears
  `completed_at`. `cancelled` closes it without a next occurrence.

## API

| Call | Purpose |
|---|---|
| `GET /api/v1/projects?status=` | Projects the caller owns or that are shared with them |
| `POST /api/v1/projects` | Create a project |
| `GET`, `PATCH`, `DELETE /api/v1/projects/{id}` | Read, change (title, description, status, shared_with), delete |
| `GET /api/v1/tasks` | Tasks the caller can see, newest first; filters: `status` (repeatable), `project_id`, `inbox`, `tag`, `due_before`, `q` (text in title or notes); paged with `before` and `limit` |
| `POST /api/v1/tasks` | Create a task |
| `GET`, `PATCH`, `DELETE /api/v1/tasks/{id}` | Read, change, delete |
| `POST /api/v1/tasks/{id}/complete` | Complete; answers the task and the next occurrence, if any |

`PATCH` changes only the fields it sends; sending `null` clears an optional
field.

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
- Members of a shared project see and edit its tasks; nobody else sees them.
- Every agent write appears in the audit log and can be undone.
