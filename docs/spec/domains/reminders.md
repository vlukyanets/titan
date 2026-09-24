# Domain: reminders

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

## Entities

| Entity | Key fields |
|---|---|
| `Reminder` | id, owner, text, fire_at, recurrence (RRULE)?, linked entity (task, event, tracker)?, status (`scheduled`, `fired`, `snoozed`, `dismissed`) |

## Delivery

- A fired reminder becomes a `reminder` notification with Snooze and Done
  actions, stored and pushed as described in
  [notifications](notifications.md).
- Events and tasks with a due time get a default reminder (configurable per user,
  default 15 minutes before).
- Approval requests from the [autonomy policy](../product.md#autonomy-policy)
  are delivered as `approval` notifications with Approve and Reject actions.

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
- A reminder fires exactly once per occurrence across the cluster.
- Snoozing from the notification works without opening the app.
