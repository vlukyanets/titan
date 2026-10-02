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
  by default), the buffer between blocks (10 minutes by default) and how long
  before its due time a task reminds its owner (`default_reminder_minutes`,
  15 by default, `null` for none; see [reminders](reminders.md#delivery)), and
  when the daily plan runs (`daily_plan_at`, 07:00 local by default, `null`
  turns it off).

## Planning behaviour

- **Daily plan** (scheduled workflow, at `daily_plan_at` in the user's time
  zone): the agent looks at open tasks, deadlines, estimates and existing
  events, has `plan_day` place time blocks inside working hours, and sends the
  user a short summary as a `plan` notification. It runs on working days
  once the user has an open task, at most once a day, and not after working
  hours. Health and finance trackers show it sums only.
- **Replanning**: when a time block ends and its task is not done, TITAN moves
  the block to the next free slot within 15 minutes, or asks first, as the
  user's policy for `calendar` `write-internal` says; it tells the user
  either way.
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

| Tool | Action class | Undo |
|---|---|---|
| `list_events` / `free_busy` | `read` | – |
| `create_event` | `write-internal` | Deletes the event |
| `update_event` (also moves) | `write-internal` | Restores the event |
| `plan_day` | `write-internal` | Deletes the blocks it placed |
| `replan_block` | `write-internal` | Moves the block back |
| Creating or changing an event with other attendees | `external` | As above |
| `delete_event` | `destructive` | – |

- `free_busy` answers the free time inside working hours, keeping the buffer,
  and the busy intervals.
- `plan_day` places time blocks on one working day. The model picks the day
  and, if it wants, the tasks and their order; without them it takes the
  user's open tasks by due date, then priority. TITAN does the placing: first
  fit inside working hours, a buffer from every event, other blocks and now,
  and 30 minutes for a task without an estimate. Tasks that already have a
  block that day are skipped, and the answer names the ones with no room.
- `replan_block` moves a time block whose task is still open to the first free
  slot of the same length, from now or the block's end, within 7 days.
- Times are given and shown in the user's time zone; a date means its local
  midnight.

## Acceptance criteria (v1)

- Users can see a day and a week view of events and time blocks on every surface.
- A recurring event keeps its local time across daylight saving changes.
- The daily plan never overlaps existing events and respects working hours and
  buffers.
- A missed time block triggers replanning within 15 minutes.
- Inviting another family member requires confirmation under the default
  policy.
