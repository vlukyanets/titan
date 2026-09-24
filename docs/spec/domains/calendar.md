# Domain: calendar and time planning

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

TITAN keeps its own calendar. There is no sync with external calendars in v1.

## Entities

| Entity | Key fields |
|---|---|
| `Event` | id, owner, title, starts_at, ends_at, all_day, location?, recurrence (RRULE)?, attendees (TITAN users), kind (`event`, `time_block`) |
| `TimeBlock` | an `Event` of kind `time_block` linked to a `task_id`, created by the planner |
| `PlanningPrefs` | per user: working hours, focus hours, buffer between blocks, days off, time zone |

## Planning behaviour

- **Daily plan** (scheduled workflow, default 07:00 in the user's time zone):
  the agent looks at open tasks, deadlines, estimates and existing events. It
  places time blocks inside working hours and sends the user a short summary.
- **Replanning**: when a time block ends and its task is not done, the agent
  proposes or applies (depending on policy) a new slot.
- **On request**: "find two hours for the tax return this week".

## Agent tools

| Tool | Action class |
|---|---|
| `calendar.list` / `calendar.free_busy` | `read` |
| `calendar.create` / `calendar.update` / `calendar.move` | `write-internal` |
| `planner.plan_day` / `planner.replan` | `write-internal` |
| Creating or changing an event with other attendees | `external` |
| `calendar.delete` | `destructive` |

## Acceptance criteria (v1)

- Users can see a day and a week view of events and time blocks on every surface.
- The daily plan never overlaps existing events and respects working hours and
  buffers.
- A missed time block triggers replanning within 15 minutes.
- Inviting another family member requires confirmation under the default
  policy.
