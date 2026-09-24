# Domain: reminders

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

## Entities

| Entity | Key fields |
|---|---|
| `Reminder` | id, owner, text, fire_at, occurs_at, recurrence (RRULE)?, linked entity (task, event, tracker)?, status (`scheduled`, `fired`, `snoozed`, `dismissed`), fired_at?, notification_id? |

- `occurs_at` is the occurrence the reminder is about; `fire_at` is when it
  fires next. They are equal until the reminder is snoozed. A recurring
  series is anchored on `occurs_at`.
- `text` is 1–500 characters. The link is a type (`task`, `event`,
  `tracker`) and an id; only links to the owner's own tasks are checked for now,
  the others arrive with their domains.

## Lifecycle

- A new reminder is `scheduled`. At `fire_at` it fires: TITAN stores a
  `reminder` notification and pushes it.
- A one-off reminder becomes `fired`. A recurring one stays `scheduled` and
  moves to its next occurrence, using the same rules as
  [recurring tasks](tasks.md#recurrence): `FREQ` is `DAILY` to `YEARLY`, no
  `COUNT`, missed occurrences are skipped, evaluated in UTC for now. When the
  rule has ended it becomes `fired`.
- Firing is a status change in the same transaction as the notification, so
  an occurrence that has fired does not fire again on the same node. Across
  nodes a reminder fires at least once
  ([ADR 0006](../../adr/0006-replicated-database-with-vectors.md)): the
  notification's id is derived from the reminder and the `fire_at` it fired
  for, so copies from two nodes are the same notification, and the scheduler's
  preferred node and leases keep such copies to real network partitions.
- **Snooze** (default 10 minutes, at most 7 days) makes a reminder fire again
  later, which is what the notification's Snooze action does. A one-off
  reminder, fired or not, gets the new `fire_at` and the status `snoozed`. A
  recurring reminder has already moved on to its next occurrence when it
  fires, so snoozing it creates a one-off copy that fires later, and the series
  stays as it is.
- **Dismiss** is the notification's Done action. A one-off reminder becomes
  `dismissed` and never fires; a recurring one only acknowledges the
  occurrence, and its series goes on. A series is stopped by deleting it or by
  clearing its `recurrence`.
- Changing `fire_at` or `recurrence` reschedules the reminder: status
  `scheduled`, `occurs_at` equal to the new `fire_at`.

## Delivery

- A fired reminder becomes a `reminder` notification whose data carries the
  `reminder_id`, with Snooze and Done actions, stored and pushed as described
  in [notifications](notifications.md).
- Events and tasks with a due time get a default reminder (configurable per user,
  default 15 minutes before).
- Approval requests from the [autonomy policy](../product.md#autonomy-policy)
  are delivered as `approval` notifications with Approve and Reject actions.

## API

| Call | Purpose |
|---|---|
| `GET /api/v1/reminders?status=&before=&limit=` | The caller's reminders, newest first |
| `POST /api/v1/reminders` | Create one (text, fire_at, recurrence?, link?) |
| `GET`, `PATCH`, `DELETE /api/v1/reminders/{id}` | Read, change text, fire_at or recurrence, delete |
| `POST /api/v1/reminders/{id}/snooze` | Snooze for `minutes` (default 10); answers the reminder that will fire, a new one for a recurring series |
| `POST /api/v1/reminders/{id}/dismiss` | The notification's Done action |

Reminders are private: someone else's reminder answers `404`.

## Agent tools

| Tool | Action class |
|---|---|
| `reminders.list` | `read` |
| `reminders.create` / `reminders.snooze` / `reminders.update` | `write-internal` |
| Creating a reminder for another user | `external` |
| `reminders.delete` | `destructive` |

## Acceptance criteria (v1)

- A reminder fires within 60 seconds of its time, even if the node that created
  it is offline, as long as one cluster node is up.
- A reminder is shown exactly once per occurrence across the cluster: copies
  fired on two nodes carry the same notification id.
- Snoozing from the notification works without opening the app.
