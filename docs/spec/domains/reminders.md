# Domain: reminders and notifications

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

## Entities

| Entity | Key fields |
|---|---|
| `Reminder` | id, owner, text, fire_at, recurrence (RRULE)?, linked entity (task, event, tracker)?, status (`scheduled`, `fired`, `snoozed`, `dismissed`) |
| `Notification` | id, user, kind (`reminder`, `approval`, `plan`, `budget`, `system`), payload, delivered_at?, read_at? |

## Delivery

- Push goes through **ntfy** with UnifiedPush on Android. The ntfy server runs
  inside the cluster, on the tailnet, so no Google services are needed.
- Each notification is also stored, so a client that was offline can list what
  it missed.
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
