# Domain: calendar and time planning

Status: **Draft v1 (thin)**. Part of the [product spec](../product.md).

TITAN keeps its own calendar. There is no sync with external calendars in v1.

## Entities

| Entity | Key fields |
|---|---|
| `Event` | id, owner, title, description, starts_at, ends_at, all_day, time_zone, location?, recurrence (RRULE)?, attendees (TITAN users), kind (`event`, `time_block`), task_id? |
| `TimeBlock` | an `Event` of kind `time_block` linked to a `task_id`, created by the planner |
| `PlanningPrefs` | per user: time zone, working hours, working days, buffer between blocks |

## Time zones

- Every user has an IANA time zone in their planning preferences, such as
  `Europe/Kyiv`; `UTC` until they set one. Family members in different zones
  each plan in their own.
- Every event has a `time_zone`, by default its owner's zone when it is
  created. Times are stored and sent in UTC; the zone decides what "the same
  time" means for repeats and what a day is for all-day events.
- A recurring event keeps its local time of day across daylight saving
  changes: a weekly 09:00 meeting in `Europe/Kyiv` stays at 09:00 there.
- An all-day event covers whole local days in its zone: `starts_at` is the
  local midnight that starts its first day and `ends_at` the local midnight
  after its last day. The server rounds the times it is given to those
  midnights.

## Events

- `ends_at` is after `starts_at`; an event lasts at most 31 days. Titles are
  1–200 characters.
- Recurrence uses the same rules as [tasks](tasks.md#recurrence) (`FREQ`
  `DAILY` to `YEARLY`, no `COUNT`, `UNTIL` allowed), with `starts_at` as the
  start of the series. Changing or cancelling a single occurrence is not in
  v1: edit the series, or end it with `UNTIL` and start a new one.
- The owner creates, changes and deletes an event. Attendees see it in their
  calendar and cannot change it. Attendees are other TITAN users; anyone else
  gets `404` for the event.
- A time block may link a task the owner can see.

## Views

- `GET /calendar/events?start=&end=` answers the occurrences that overlap the
  window, each with its own `starts_at` and `ends_at` and the id of its event.
  A window is at most 92 days, so a day, a week or a month view is one call.
- `GET /calendar/busy?start=&end=` answers the caller's busy intervals in the
  window, merged, from their own events and the ones they attend. The planner
  places blocks outside them.

## Planning preferences

- `GET` and `PUT /calendar/prefs`: time zone, working hours (local start and
  end, 09:00–17:00 by default), working days (ISO weekdays, Monday to Friday
  by default) and the buffer between blocks (10 minutes by default).

## Planning behaviour

- **Daily plan** (scheduled workflow, default 07:00 in the user's time zone):
  the agent looks at open tasks, deadlines, estimates and existing events. It
  places time blocks inside working hours and sends the user a short summary.
- **Replanning**: when a time block ends and its task is not done, the agent
  proposes or applies (depending on policy) a new slot.
- **On request**: "find two hours for the tax return this week".

## API

| Call | Purpose |
|---|---|
| `GET /api/v1/calendar/events?start=&end=` | Occurrences in a window (own and attended) |
| `POST /api/v1/calendar/events` | Create an event |
| `GET`, `PATCH`, `DELETE /api/v1/calendar/events/{id}` | Read, change (owner only), delete (owner only) |
| `GET /api/v1/calendar/busy?start=&end=` | The caller's merged busy intervals |
| `GET`, `PUT /api/v1/calendar/prefs` | The caller's planning preferences |

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
- A recurring event keeps its local time across daylight saving changes.
- The daily plan never overlaps existing events and respects working hours and
  buffers.
- A missed time block triggers replanning within 15 minutes.
- Inviting another family member requires confirmation under the default
  policy.
